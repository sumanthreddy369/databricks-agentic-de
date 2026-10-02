"""Glue + guardrails around Databricks Genie: the ask_genie tool.

Genie (the platform's natural-language-to-SQL agent) does the analysis. This
module is the layer around it that companies build themselves:

Before Genie sees the question
- PHI-request policy: a question asking for patient names or record numbers
  is refused here and never sent. Unity Catalog masks would redact them
  anyway; refusing up front means the request isn't even attempted.

After Genie answers, before the model or a person sees it
- Schema check: Genie's SQL may only read the governed Gold schema. A result
  from SQL that touches bronze/silver/ops (which the agent's grants shouldn't
  allow) is withheld.
- Masking backstop: any result column named like a masked column
  (common.contracts.MASKED_COLUMNS, any table) is redacted, and any raw value
  from such a column that appears in Genie's text answer is redacted there
  too. This catches a misconfigured UC mask instead of relying on it.
- Small-cell suppression: count-like columns with values 1-10 are suppressed
  (MIN_CELL_SIZE). Genie's SQL is free-form, so "count-like" is a name
  heuristic here, unlike aggregate_gold_table where it's exact.
- Size cap on rows; then the orchestrator's injection scan, size guard, and
  audit log apply as for every tool.

Not configured (no DATABRICKS_HOST/TOKEN/GENIE_SPACE_ID): returns an
explicit "not configured, use the fallback tools" result, never an error -
the same no-op-unless-configured pattern as the rest of agent/. Tested only
against a mocked Genie API; never called a real Genie space.
"""

import json
import re

from agent.databricks_client import DatabricksClient, DatabricksError, load_config
from agent.llm import ToolResult
from agent.tools.governance_guard import REDACTED
from common.contracts import GOLD_SCHEMA, MASKED_COLUMNS, MAX_TOOL_RESULT_ROWS, MIN_CELL_SIZE

_ALL_MASKED = {column for columns in MASKED_COLUMNS.values() for column in columns}

_PHI_REQUEST = re.compile(
    r"\b(full[_ ]?names?|mrns?|medical record numbers?|patient'?s? names?"
    r"|names? of (the |every |each |all )?patients?)\b",
    re.IGNORECASE,
)
_COUNT_LIKE = re.compile(r"(^|_)(count|cnt|num|n|patients|census|total)(_|$)", re.IGNORECASE)
_TABLE_REF = re.compile(r"\b(?:from|join)\s+([`\w.]+)", re.IGNORECASE)
_DISALLOWED_SCHEMAS = ("bronze", "silver", "ops", "information_schema", "system")


def _result(payload: dict, *, is_error: bool = False) -> ToolResult:
    return ToolResult(tool_use_id="", content=json.dumps(payload, default=str), is_error=is_error)


def _sql_outside_gold(sql: str | None) -> list[str]:
    if not sql:
        return []
    bad = []
    for ref in _TABLE_REF.findall(sql):
        parts = ref.replace("`", "").lower().split(".")
        if any(part in _DISALLOWED_SCHEMAS for part in parts[:-1]):
            bad.append(ref)
        elif len(parts) == 3 and ".".join(parts[:2]) != GOLD_SCHEMA:
            bad.append(ref)
    return bad


def _guard_rows(rows: list[dict]) -> tuple[list[dict], list[str], set[str], int]:
    """Returns (guarded_rows, masked_column_names, raw_phi_values, suppressed_cell_count)."""
    masked_columns, raw_values, suppressed, guarded = set(), set(), 0, []
    for row in rows[:MAX_TOOL_RESULT_ROWS]:
        out = {}
        for column, value in row.items():
            name = column.lower()
            if name in _ALL_MASKED:
                masked_columns.add(column)
                if value not in (None, REDACTED):
                    raw_values.add(str(value))
                out[column] = REDACTED
            elif (
                _COUNT_LIKE.search(name)
                and isinstance(value, int)
                and not isinstance(value, bool)
                and 0 < value < MIN_CELL_SIZE
            ):
                out[column] = None
                out[f"{column}_suppressed"] = True
                suppressed += 1
            else:
                out[column] = value
        guarded.append(out)
    return guarded, sorted(masked_columns), raw_values, suppressed


def ask_genie(question: str, *, client: DatabricksClient | None = None) -> ToolResult:
    if not question or not question.strip():
        return _result({"error": "question is empty"}, is_error=True)

    if _PHI_REQUEST.search(question):
        return _result(
            {
                "ok": False,
                "refused": True,
                "reason": "The question asks for patient identifiers (names / MRNs), which are masked by policy. "
                "It was not sent to Genie.",
            }
        )

    if client is None:
        config = load_config()
        if config is None or not config.genie_space_id:
            return _result(
                {
                    "ok": False,
                    "configured": False,
                    "message": "Genie is not configured (DATABRICKS_HOST, DATABRICKS_TOKEN, GENIE_SPACE_ID). "
                    "Answer with aggregate_gold_table / query_gold_table instead.",
                }
            )
        client = DatabricksClient(config)

    try:
        answer = client.genie_ask(question)
    except DatabricksError as exc:
        return _result({"ok": False, "source": "genie", "error": str(exc)}, is_error=True)

    outside = _sql_outside_gold(answer.get("sql"))
    if outside:
        return _result(
            {
                "ok": False,
                "source": "genie",
                "withheld": True,
                "reason": f"Genie's SQL read outside {GOLD_SCHEMA}: {outside}. Result withheld; escalate.",
                "sql": answer.get("sql"),
            }
        )

    rows, masked_columns, raw_values, suppressed = _guard_rows(answer.get("rows") or [])
    text = answer.get("text") or ""
    leaked_into_text = 0
    for value in raw_values:
        if value and value in text:
            text = text.replace(value, REDACTED)
            leaked_into_text += 1

    payload = {
        "ok": True,
        "source": "genie",
        "answer_text": text,
        "sql": answer.get("sql"),
        "rows": rows,
        "truncated": len(answer.get("rows") or []) > MAX_TOOL_RESULT_ROWS,
        "guardrails": {
            "masked_columns": masked_columns,
            "phi_values_redacted_from_text": leaked_into_text,
            "suppressed_cells": suppressed,
            "min_cell_size": MIN_CELL_SIZE,
            "sql_schema_check": "passed",
        },
        "conversation_id": answer.get("conversation_id"),
    }
    return _result(payload)
