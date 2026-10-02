"""Minimal Databricks REST client for the live-workspace paths of the DA-mode
query tool (`agent/tools/data_query.py`) and the DE-mode pipeline-health
tools (`agent/tools/pipeline_health_live.py`).

STATUS: written against Databricks' documented REST API shapes (SQL
Statement Execution API, Pipelines API) and tested only against a mocked
`httpx` transport — it has NOT been run against a live workspace in this
environment. Treat a passing test here as proof of the request/response
handling, not of the workspace integration.

Same no-op-unless-configured pattern as `agent/secrets.py`,
`agent/otel.py`, and `agent/llm.py:Tracer`: `load_config()` returns `None`
unless `DATABRICKS_HOST` and `DATABRICKS_TOKEN` are both set, and callers
fall back to their local stand-ins (DuckDB, the JSON state file) — no
import, no network call, nothing to configure for local dev, CI, or the
test suite. The token is resolved through `agent.secrets.get_secret`, so it
comes from GCP Secret Manager when `GCP_PROJECT_ID` is set and never has to
live in a committed file.

Deliberately plain `httpx` rather than `databricks-sdk` or
`databricks-sql-connector`: two small endpoint families are all this needs,
`httpx` is already installed (the Anthropic SDK depends on it), and a
constructor-injected `transport` lets tests assert the exact requests sent
with zero network calls — the same dependency-injection-over-globals
pattern the rest of `agent/` uses.
"""

import json
import os
import time
from dataclasses import dataclass, field

import httpx
import structlog

from agent.secrets import get_secret

logger = structlog.get_logger(__name__)

DEFAULT_TIMEOUT_SECONDS = 60.0

# The Statement Execution API waits up to this long for a result inline
# before the call returns; `on_wait_timeout=CANCEL` means a slow query is
# cancelled rather than left running with no one polling for it.
STATEMENT_WAIT_TIMEOUT = "30s"

GENIE_TIMEOUT_SECONDS = 60.0
GENIE_POLL_SECONDS = 2.0
GENIE_FAILED_STATUSES = {"FAILED", "CANCELLED", "QUERY_RESULT_EXPIRED"}

# JSON_ARRAY results come back as strings; these Databricks SQL type names
# are converted back to Python numbers/bools so the agent sees the same
# shapes the DuckDB stand-in returns. Everything else (STRING, DATE,
# TIMESTAMP, ...) stays a string, which is how json.dumps(default=str) would
# render it anyway.
_INT_TYPES = {"BYTE", "SHORT", "INT", "LONG"}
_FLOAT_TYPES = {"FLOAT", "DOUBLE", "DECIMAL"}


class DatabricksError(RuntimeError):
    """A configured Databricks call failed (HTTP error, failed statement)."""


@dataclass(frozen=True)
class DatabricksConfig:
    host: str
    token: str = field(repr=False)
    warehouse_id: str | None = None
    # Logical pipeline name (the bundle resource key, e.g.
    # "streaming_patient_pipeline") -> pipeline ID. An explicit map rather
    # than a lookup by name, because a development-mode bundle deploy
    # prefixes the deployed name with "[dev <user>]".
    pipeline_ids: dict[str, str] = field(default_factory=dict)
    # Genie space the glue layer sends analyst questions to (agent/tools/genie.py).
    genie_space_id: str | None = None


def load_config() -> DatabricksConfig | None:
    """Returns the live-workspace config, or `None` if it isn't configured.

    Reads `DATABRICKS_HOST`, `DATABRICKS_TOKEN` (via Secret Manager when
    available), `DATABRICKS_WAREHOUSE_ID` (DA-mode queries) and
    `DATABRICKS_PIPELINE_IDS` (DE-mode, a JSON object of name -> ID), and
    `GENIE_SPACE_ID` (the Genie space DA-mode questions are routed to).
    """
    host = os.environ.get("DATABRICKS_HOST")
    if not host:
        return None
    token = get_secret("DATABRICKS_TOKEN")
    if not token:
        return None

    pipeline_ids: dict[str, str] = {}
    raw_ids = os.environ.get("DATABRICKS_PIPELINE_IDS", "")
    if raw_ids:
        try:
            parsed = json.loads(raw_ids)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()):
            pipeline_ids = parsed
        else:
            logger.warning("ignoring_malformed_databricks_pipeline_ids")

    return DatabricksConfig(
        host=host.rstrip("/"),
        token=token,
        warehouse_id=os.environ.get("DATABRICKS_WAREHOUSE_ID") or None,
        pipeline_ids=pipeline_ids,
        genie_space_id=os.environ.get("GENIE_SPACE_ID") or None,
    )


class DatabricksClient:
    def __init__(
        self,
        config: DatabricksConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.config = config
        self._http = httpx.Client(
            base_url=config.host,
            headers={"Authorization": f"Bearer {config.token}"},
            timeout=timeout,
            transport=transport,
        )

    def _request(self, method: str, path: str, **kwargs) -> dict:
        response = self._http.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise DatabricksError(f"{method} {path} -> HTTP {response.status_code}: {response.text[:500]}")
        return response.json() if response.content else {}

    # --- SQL Statement Execution API ----------------------------------------

    def execute_statement(self, statement: str, parameters: list[dict] | None = None) -> list[dict]:
        """Runs `statement` on the configured SQL warehouse and returns rows
        as dicts. Values are bound as named parameters (`:p0`), never
        interpolated into the SQL text.

        Only the first inline result chunk is read. Callers must bound the
        row count with a LIMIT (data_query does), which keeps the result far
        below the inline chunk size.
        """
        if not self.config.warehouse_id:
            raise DatabricksError("DATABRICKS_WAREHOUSE_ID is not set")
        body = {
            "warehouse_id": self.config.warehouse_id,
            "statement": statement,
            "parameters": parameters or [],
            "wait_timeout": STATEMENT_WAIT_TIMEOUT,
            "on_wait_timeout": "CANCEL",
            "disposition": "INLINE",
            "format": "JSON_ARRAY",
        }
        payload = self._request("POST", "/api/2.0/sql/statements/", json=body)
        status = payload.get("status", {})
        if status.get("state") != "SUCCEEDED":
            message = status.get("error", {}).get("message", "no error message")
            raise DatabricksError(f"statement {status.get('state', 'UNKNOWN')}: {message}")

        return _rows(payload)

    # --- Genie Conversation API ----------------------------------------------
    #
    # Shapes follow the documented Genie Conversation API: start a
    # conversation with a question, poll the message until it completes, then
    # fetch the query result of any attachment that carries SQL. Never called
    # against a real Genie space in this environment.

    def genie_ask(
        self,
        question: str,
        *,
        timeout_s: float = GENIE_TIMEOUT_SECONDS,
        poll_s: float = GENIE_POLL_SECONDS,
        sleep=time.sleep,
    ) -> dict:
        """Asks the configured Genie space one question. Returns
        {"conversation_id", "message_id", "text", "sql", "rows"} - rows is
        None when Genie answered without running a query."""
        space = self.config.genie_space_id
        if not space:
            raise DatabricksError("GENIE_SPACE_ID is not set")
        started = self._request(
            "POST", f"/api/2.0/genie/spaces/{space}/start-conversation", json={"content": question}
        )
        conversation_id = started.get("conversation_id") or started.get("conversation", {}).get("id")
        message_id = started.get("message_id") or started.get("message", {}).get("id")
        if not conversation_id or not message_id:
            raise DatabricksError("Genie did not return a conversation and message id")

        base = f"/api/2.0/genie/spaces/{space}/conversations/{conversation_id}/messages/{message_id}"
        waited = 0.0
        while True:
            message = self._request("GET", base)
            status = message.get("status", "")
            if status == "COMPLETED":
                break
            if status in GENIE_FAILED_STATUSES:
                error = (message.get("error") or {}).get("error") or status
                raise DatabricksError(f"Genie message {status}: {error}")
            if waited >= timeout_s:
                raise DatabricksError(
                    f"Genie did not answer within {timeout_s:.0f}s (last status {status or 'unknown'})"
                )
            sleep(poll_s)
            waited += poll_s

        text_parts, sql, rows = [], None, None
        for attachment in message.get("attachments") or []:
            if attachment.get("text"):
                text_parts.append(attachment["text"].get("content", ""))
            if attachment.get("query"):
                query = attachment["query"]
                sql = query.get("query")
                if query.get("description"):
                    text_parts.append(query["description"])
                attachment_id = attachment.get("attachment_id") or attachment.get("id")
                result = self._request("GET", f"{base}/attachments/{attachment_id}/query-result")
                rows = _rows(result.get("statement_response") or result)
        return {
            "conversation_id": conversation_id,
            "message_id": message_id,
            "text": "\n".join(t for t in text_parts if t),
            "sql": sql,
            "rows": rows,
        }

    # --- Pipelines API --------------------------------------------------------

    def get_pipeline(self, pipeline_id: str) -> dict:
        return self._request("GET", f"/api/2.0/pipelines/{pipeline_id}")

    def list_pipeline_events(self, pipeline_id: str, max_results: int = 250) -> list[dict]:
        """Most recent events first. The events API's `filter` only supports
        level/timestamp/id, so callers filter on `event_type` themselves."""
        payload = self._request(
            "GET",
            f"/api/2.0/pipelines/{pipeline_id}/events",
            params={"max_results": max_results, "order_by": "timestamp desc"},
        )
        return payload.get("events", [])

    def start_update(self, pipeline_id: str) -> dict:
        """Starts a normal, incremental pipeline update. `full_refresh` is
        pinned to False: a full refresh truncates and recomputes tables, the
        kind of destructive action no tool in this project may take (see
        AGENTS.md and tests/test_tool_allowlist.py)."""
        return self._request("POST", f"/api/2.0/pipelines/{pipeline_id}/updates", json={"full_refresh": False})


def _rows(payload: dict) -> list[dict]:
    """Rows from a SQL statement response (JSON_ARRAY format), typed by the
    manifest. Shared by execute_statement and Genie query results."""
    columns = payload.get("manifest", {}).get("schema", {}).get("columns", [])
    data = (payload.get("result") or {}).get("data_array") or []
    return [
        {
            col["name"]: _convert(value, col.get("type_name", "STRING"))
            for col, value in zip(columns, row, strict=True)
        }
        for row in data
    ]


def _convert(value, type_name: str):
    if value is None:
        return None
    type_name = type_name.upper()
    if type_name in _INT_TYPES:
        return int(value)
    if type_name in _FLOAT_TYPES:
        return float(value)
    if type_name == "BOOLEAN":
        return value.lower() == "true" if isinstance(value, str) else bool(value)
    return value
