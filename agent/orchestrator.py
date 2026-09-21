"""The orchestrator agent: one coordinator, two modes.

`OrchestratorAgent.handle()` routes an incoming request to either:
- DE mode: pipeline-health tools (agent/tools/pipeline_health.py), for
  keeping the streaming pipeline itself healthy with no human paged in
  unless truly necessary.
- DA mode: the data-query tool (agent/tools/data_query.py), for answering
  clinical/ops questions over governed Gold tables.

The critical wiring lives in `_dispatch`: every `query_gold_table` result is
masked via `governance_guard.enforce_masking` BEFORE it is ever placed into a
`ToolResult.content` (and therefore before it ever enters `messages`), and
every tool result's content is scanned via `governance_guard.scan_for_injection`
and wrapped in `<untrusted_data>` tags when flagged. Raw, unmasked rows never
touch `messages` at any point.
"""

import json
from pathlib import Path
from typing import Literal

from agent.llm import Claude, ToolResult
from agent.prompts import MODE_ROUTER_PROMPT, SYSTEM_PROMPT_DA, SYSTEM_PROMPT_DE
from agent.state import OrchestratorResult
from agent.tools import data_query, governance_guard, pipeline_health

DE_TOOLS = [
    {
        "name": "check_expectation_metrics",
        "description": "Check DLT expectation failure counts across all tables.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "check_job_status",
        "description": "Check the status of pipeline jobs.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "detect_schema_drift",
        "description": "Diff the observed patient_events schema against the contract.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "quarantine_bad_records",
        "description": "Clear a failing expectation's failure count by quarantining bad records.",
        "input_schema": {
            "type": "object",
            "properties": {"table": {"type": "string"}, "expectation": {"type": "string"}},
            "required": ["table", "expectation"],
        },
    },
    {
        "name": "restart_pipeline",
        "description": "Restart a named pipeline/job.",
        "input_schema": {
            "type": "object",
            "properties": {"pipeline_name": {"type": "string"}},
            "required": ["pipeline_name"],
        },
    },
    {
        "name": "notify_and_page",
        "description": "Escalate to a human on-call engineer. Use for hard-stop failures only.",
        "input_schema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
    },
]

DA_TOOLS = [
    {
        "name": "query_gold_table",
        "description": "Query a governed Gold table. Rows are masked per platform policy before you see them.",
        "input_schema": {
            "type": "object",
            "properties": {
                "table": {"type": "string"},
                "filters": {"type": "object"},
            },
            "required": ["table"],
        },
    },
]

ORCHESTRATOR_ROLE = "orchestrator_agent"


class OrchestratorAgent:
    def __init__(
        self,
        claude: Claude | None = None,
        state_path: Path | str = "data/state/pipeline_state.json",
        duckdb_path: Path | str | None = None,
        seed_sql_path: Path | str | None = None,
    ) -> None:
        self.claude = claude if claude is not None else Claude()
        self.state_path = Path(state_path)
        self.duckdb_path = Path(duckdb_path) if duckdb_path else None
        self.seed_sql_path = Path(seed_sql_path) if seed_sql_path else None
        self._tool_calls: list[str] = []
        self._remediated = False

    def handle(self, request: str, mode: Literal["auto", "de", "da"] = "auto") -> OrchestratorResult:
        resolved_mode: Literal["de", "da"] = self._route(request) if mode == "auto" else mode
        self._tool_calls = []
        self._remediated = False

        if resolved_mode == "de":
            system, tools = SYSTEM_PROMPT_DE, DE_TOOLS
        else:
            system, tools = SYSTEM_PROMPT_DA, DA_TOOLS

        messages = [{"role": "user", "content": request}]
        answer = self.claude.run_tool_loop(
            system=system,
            messages=messages,
            tools=tools,
            dispatch=self._dispatch,
        )

        return OrchestratorResult(
            mode=resolved_mode,
            answer=answer,
            tool_calls=list(self._tool_calls),
            remediated=self._remediated,
        )

    def _route(self, request: str) -> Literal["de", "da"]:
        """One small Claude call classifying the request; defensively
        defaults to "da" on ANY parse failure. Mirrors the exact pattern used
        by the sibling `abhay` project's ManagerAgent._route.
        """
        try:
            raw = self.claude.run_tool_loop(
                system=MODE_ROUTER_PROMPT,
                messages=[{"role": "user", "content": request}],
                tools=[],
                dispatch=self._dispatch,
            )
            start, end = raw.find("{"), raw.rfind("}")
            parsed = json.loads(raw[start : end + 1])
            mode = parsed.get("mode", "da")
            return mode if mode in ("de", "da") else "da"
        except Exception:
            return "da"

    def _dispatch(self, tool_name: str, tool_input: dict) -> ToolResult:
        self._tool_calls.append(tool_name)

        if tool_name == "query_gold_table":
            result = self._dispatch_query_gold_table(tool_input)
        elif hasattr(pipeline_health, tool_name):
            result = self._dispatch_pipeline_health(tool_name, tool_input)
        else:
            result = ToolResult(tool_use_id="", content=f"Unknown tool: {tool_name}", is_error=True)

        return self._apply_injection_guard(result)

    def _dispatch_query_gold_table(self, tool_input: dict) -> ToolResult:
        table = tool_input.get("table", "")
        filters = tool_input.get("filters")
        kwargs = {}
        if self.duckdb_path is not None:
            kwargs["db_path"] = self.duckdb_path
        if self.seed_sql_path is not None:
            kwargs["seed_sql_path"] = self.seed_sql_path

        raw_result = data_query.query_gold_table(table, filters, **kwargs)
        if raw_result.is_error:
            return raw_result

        # CRITICAL: raw rows are masked here, before they are ever placed into
        # a ToolResult.content that gets appended to `messages`. The raw,
        # unmasked rows (raw_result.content / `rows` below) must never be
        # returned from this method.
        rows = json.loads(raw_result.content)
        masked_rows = governance_guard.enforce_masking(table, rows, role=ORCHESTRATOR_ROLE)
        return ToolResult(tool_use_id="", content=json.dumps(masked_rows, default=str))

    def _dispatch_pipeline_health(self, tool_name: str, tool_input: dict) -> ToolResult:
        fn = getattr(pipeline_health, tool_name)
        result = fn(self.state_path, **tool_input)
        if tool_name in ("quarantine_bad_records", "restart_pipeline") and not result.is_error:
            self._remediated = True
        return result

    @staticmethod
    def _apply_injection_guard(result: ToolResult) -> ToolResult:
        if governance_guard.scan_for_injection(result.content):
            wrapped = f"<untrusted_data>{result.content}</untrusted_data>"
            return ToolResult(tool_use_id=result.tool_use_id, content=wrapped, is_error=result.is_error)
        return result
