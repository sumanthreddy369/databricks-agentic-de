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

DE-mode tool calls (the pipeline-health checks, the two remediation tools,
notify_and_page, and score_vitals_anomaly) are dispatched through
`agent/mcp_bridge.py`'s `MCPToolBridge` — a real MCP (Model Context Protocol)
client that talks to `mcp_server/server.py` over stdio — instead of calling
`agent.tools.pipeline_health`/`agent.tools.anomaly_score` functions directly
in-process. DA-mode's `query_gold_table` deliberately stays a direct
in-process call: masking must be the very next thing that happens to a raw
row (see `_dispatch_query_gold_table` below), and routing that specific call
through an extra process boundary first would add risk/complexity to the
single most safety-critical path in this codebase for no real benefit — see
that method's own comment. Every guardrail described below runs exactly the
same way regardless of which path a given tool call takes; the MCP switch is
a transport-layer change to a helper function's call, not a change to the
guardrail wiring itself. `tests/test_mcp_server_tools.py` re-proves the
masking and audit-trail guarantees hold with MCP-routed DE-mode dispatch.

Guardrails added on top of that (see docs/architecture.md's "Guardrails"
section for the full list with test pointers):
- Escalation ceiling: `_dispatch_quarantine` tracks consecutive successful
  quarantine attempts per (table, expectation) in the state file and forces
  `notify_and_page` once `ESCALATION_CEILING` is reached, in code, not just
  via prompt instruction.
- Kill switch: `_autonomous_remediation_enabled` gates `quarantine_bad_records`
  and `restart_pipeline` on the state file's `autonomous_remediation_enabled`
  flag (default true); read-only health checks are never gated.
- Groundedness: a zero-row `query_gold_table` result is short-circuited to a
  fixed refusal string instead of flowing to the model as ordinary data.
- Input/output size limits: tool results are capped (`MAX_TOOL_RESULT_ROWS`,
  `MAX_TOOL_RESULT_CHARS`) before ever reaching `messages`.
- Immutable audit trail: every `_dispatch` call appends one line to an
  append-only JSONL log (never rewritten/truncated by normal operation).
- Graceful degradation: `handle()` never lets a Claude API failure crash the
  caller.
"""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import structlog

from agent.llm import Claude, ToolResult
from agent.mcp_bridge import MCPToolBridge, get_default_bridge
from agent.prompts import MODE_ROUTER_PROMPT, SYSTEM_PROMPT_DA, SYSTEM_PROMPT_DE
from agent.state import OrchestratorResult
from agent.tools import data_query, governance_guard, pipeline_health
from common.contracts import MAX_TOOL_RESULT_CHARS, MAX_TOOL_RESULT_ROWS

logger = structlog.get_logger(__name__)

# Offered in both modes: matches what the agent observed against the
# real-time problem catalog (agent/knowledge/problem_catalog.yaml) and returns
# the expected response and autonomy level. Read-only, no data access.
LOOKUP_PROBLEM_TOOL = {
    "name": "lookup_problem",
    "description": (
        "Match what you observed (e.g. 'no events for 20 minutes but jobs are green', 'heart rate 0 readings') "
        "or a problem ID (e.g. 'K2') against the catalog of real-time data problems on this platform. Returns "
        "each match's ID, expected response, and the autonomy level that limits what you may do about it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string"}, "problem_id": {"type": "string"}},
    },
}

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
    {
        "name": "score_vitals_anomaly",
        "description": (
            "Score one multi-vital reading (heart_rate, spo2, resp_rate, temp_c, sbp, dbp) for JOINT "
            "anomaly risk via a local ONNX IsolationForest model — catches a plausible-looking "
            "combination of vitals that a single-column range check would miss."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"vitals": {"type": "object"}},
            "required": ["vitals"],
        },
    },
    LOOKUP_PROBLEM_TOOL,
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
    {
        "name": "aggregate_gold_table",
        "description": (
            "Compute count, count_distinct, avg, min, max, or sum over a governed Gold table in SQL, optionally "
            "filtered and grouped (up to 3 group_by columns). Use this for every how-many / average / trend "
            "question: query_gold_table returns at most 500 rows, so counting its rows gives wrong answers. "
            "Groups covering fewer than 11 patients come back suppressed (value null). as_of (ISO timestamp) "
            "only applies to fct_encounter_history and selects the encounter versions in effect at that moment."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "table": {"type": "string"},
                "metric": {"type": "string", "enum": ["count", "count_distinct", "avg", "min", "max", "sum"]},
                "column": {"type": "string"},
                "filters": {"type": "object"},
                "where": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "column": {"type": "string"},
                            "op": {"type": "string", "enum": ["=", "!=", "<", "<=", ">", ">="]},
                            "value": {},
                        },
                        "required": ["column", "op", "value"],
                    },
                },
                "group_by": {"type": "array", "items": {"type": "string"}},
                "as_of": {"type": "string"},
            },
            "required": ["table", "metric"],
        },
    },
    LOOKUP_PROBLEM_TOOL,
]

ORCHESTRATOR_ROLE = "orchestrator_agent"

# Escalation-ceiling guardrail: if a (table, expectation) pair has already
# been "fixed" this many times and the failure is still present, the next
# quarantine attempt is refused in code and forced to notify_and_page
# instead — the model cannot be trusted to self-limit a retry loop.
ESCALATION_CEILING = 3

# Groundedness guardrail: fixed, deterministic text returned instead of an
# empty row list, so the model is never handed "no rows" framed as if it were
# ordinary queryable data it could speculate about.
GROUNDEDNESS_REFUSAL = "No matching rows found for this query — do not speculate."

# Graceful-degradation guardrail: the fixed answer handle() returns when the
# Claude call path raises. A named constant so callers (agent/healthcheck.py)
# can tell "the agent couldn't run" apart from a real answer.
AGENT_UNAVAILABLE_ANSWER = (
    "Agent unavailable — Delta Live Tables expectations continue enforcing "
    "data quality independently of this agent."
)

DEFAULT_AUDIT_LOG_PATH = "data/state/audit_log.jsonl"

_REMEDIATION_TOOLS = ("quarantine_bad_records", "restart_pipeline")


def detect_anomalous_activity(
    audit_log_path: Path | str,
    window_minutes: int = 60,
    escalation_threshold: int = 5,
    *,
    now: datetime | None = None,
) -> bool:
    """Pure function: reads the append-only audit log and returns True if the
    number of `notify_and_page` calls in the trailing `window_minutes` (up to
    `now`, defaulting to the real current time) exceeds `escalation_threshold`.

    This is a behavioral-anomaly signal, not an action — either a real
    widespread incident is happening (many independent genuine failures) or
    the agent itself is misbehaving (e.g. stuck in an escalation loop); either
    way, exceeding the threshold is worth a human's attention beyond the
    individual pages already sent.

    `now` is accepted explicitly (rather than always reading the wall clock)
    so callers — most notably tests — can drive this deterministically
    against a synthetic audit log with controlled timestamps.
    """
    path = Path(audit_log_path)
    if not path.exists():
        return False

    reference = now or datetime.now(UTC)
    cutoff = reference - timedelta(minutes=window_minutes)

    count = 0
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("tool") != "notify_and_page":
            continue
        try:
            ts = datetime.fromisoformat(entry["ts"])
        except (KeyError, ValueError):
            continue
        if ts >= cutoff:
            count += 1

    return count > escalation_threshold


class OrchestratorAgent:
    def __init__(
        self,
        claude: Claude | None = None,
        state_path: Path | str = "data/state/pipeline_state.json",
        duckdb_path: Path | str | None = None,
        seed_sql_path: Path | str | None = None,
        audit_log_path: Path | str | None = None,
        mcp_bridge: MCPToolBridge | None = None,
    ) -> None:
        self.claude = claude if claude is not None else Claude()
        # Absolute, because this path is handed to the MCP server subprocess,
        # whose working directory is the package location (the repo root
        # from source, site-packages from an installed wheel), not ours. A
        # relative path would silently point at a different file there.
        self.state_path = Path(state_path).resolve()
        self.duckdb_path = Path(duckdb_path) if duckdb_path else None
        self.seed_sql_path = Path(seed_sql_path) if seed_sql_path else None
        self.audit_log_path = Path(audit_log_path or os.environ.get("AUDIT_LOG_PATH", DEFAULT_AUDIT_LOG_PATH))
        # DE-mode tool calls are dispatched through this MCP bridge (see
        # agent/mcp_bridge.py and this module's own docstring) instead of
        # calling agent.tools.pipeline_health/anomaly_score functions
        # in-process directly. Defaults to the process-wide shared bridge
        # (one MCP server subprocess for the whole process, not one per
        # OrchestratorAgent) — tests that want an isolated/fake bridge inject
        # their own via this parameter.
        self.mcp_bridge = mcp_bridge if mcp_bridge is not None else get_default_bridge()
        self._tool_calls: list[str] = []
        self._remediated = False
        self._current_mode: str = "unknown"

    def handle(self, request: str, mode: Literal["auto", "de", "da"] = "auto") -> OrchestratorResult:
        resolved_mode: Literal["de", "da"] = self._route(request) if mode == "auto" else mode
        self._current_mode = resolved_mode
        self._tool_calls = []
        self._remediated = False

        if resolved_mode == "de":
            system, tools = SYSTEM_PROMPT_DE, DE_TOOLS
        else:
            system, tools = SYSTEM_PROMPT_DA, DA_TOOLS

        messages = [{"role": "user", "content": request}]
        try:
            answer = self.claude.run_tool_loop(
                system=system,
                messages=messages,
                tools=tools,
                dispatch=self._dispatch,
            )
        except Exception:
            # Graceful-degradation guardrail, deliberate: if the Claude API
            # call path raises (network outage, auth failure, rate limit
            # exhausted past retries, etc.), we do NOT let that crash the
            # caller. The pipeline's own DLT hard-stop expectations (e.g.
            # `known_event_type` in pipeline/03_silver/silver_encounters.py)
            # keep enforcing data-quality/contract guarantees completely
            # independently of whether this agent is reachable — the agent is
            # an automation/convenience layer on top of that guarantee, never
            # a replacement for it, so its own unavailability must fail safe.
            logger.error("claude_call_failed_graceful_degradation", mode=resolved_mode)
            return OrchestratorResult(
                mode=resolved_mode,
                answer=AGENT_UNAVAILABLE_ANSWER,
                tool_calls=list(self._tool_calls),
                remediated=False,
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
        elif tool_name == "aggregate_gold_table":
            result = self._dispatch_aggregate_gold_table(tool_input)
        elif tool_name in _REMEDIATION_TOOLS:
            # Blast-radius note: these are the ONLY two tools anywhere in
            # this codebase that mutate pipeline/job state (see
            # tests/test_tool_allowlist.py) — both are gated by the kill
            # switch and, for quarantine, the escalation ceiling.
            result = (
                self._dispatch_quarantine(tool_input)
                if tool_name == "quarantine_bad_records"
                else self._dispatch_restart(tool_input)
            )
        elif tool_name in ("score_vitals_anomaly", "lookup_problem"):
            result = self.mcp_bridge.dispatch(tool_name, tool_input)
        elif hasattr(pipeline_health, tool_name):
            result = self._dispatch_pipeline_health(tool_name, tool_input)
        else:
            result = ToolResult(tool_use_id="", content=f"Unknown tool: {tool_name}", is_error=True)

        result = self._apply_injection_guard(result)
        result = self._apply_output_size_guard(result)
        self._append_audit_log(tool_name, tool_input, result)
        return result

    def _dispatch_query_gold_table(self, tool_input: dict) -> ToolResult:
        table = tool_input.get("table", "")
        filters = tool_input.get("filters")
        kwargs = {}
        if self.duckdb_path is not None:
            kwargs["db_path"] = self.duckdb_path
        if self.seed_sql_path is not None:
            kwargs["seed_sql_path"] = self.seed_sql_path

        # CRITICAL, deliberate design choice: this calls agent.tools.data_query
        # directly in-process, NOT through self.mcp_bridge, even though
        # DE-mode's tools all go through the MCP bridge now (see this
        # module's docstring). Masking must run as the very next thing that
        # happens to a raw row after it's read — routing it through a real
        # MCP round trip first would mean an extra serialize/deserialize hop
        # for unmasked PHI to cross before enforce_masking() ever sees it,
        # for zero benefit (query_gold_table has no side effects to
        # standardize, unlike the DE remediation tools). mcp_server/server.py
        # DOES expose a query_gold_table MCP tool for protocol-surface
        # completeness/testing (tests/test_mcp_server_tools.py exercises it),
        # it is simply never the path this production dispatch method takes.
        raw_result = data_query.query_gold_table(table, filters, **kwargs)
        if raw_result.is_error:
            return raw_result

        # CRITICAL: raw rows are masked here, before they are ever placed into
        # a ToolResult.content that gets appended to `messages`. The raw,
        # unmasked rows (raw_result.content / `rows` below) must never be
        # returned from this method.
        rows = json.loads(raw_result.content)

        if not rows:
            # Groundedness guardrail: an empty result set must never flow to
            # the model dressed up as ordinary data — that invites confident
            # speculation built on what might just be a typo'd filter. Return
            # a fixed, deterministic refusal instead.
            return ToolResult(tool_use_id="", content=GROUNDEDNESS_REFUSAL)

        masked_rows = governance_guard.enforce_masking(table, rows, role=ORCHESTRATOR_ROLE)

        if len(masked_rows) > MAX_TOOL_RESULT_ROWS:
            logger.warning(
                "truncating_tool_result_rows",
                table=table,
                row_count=len(masked_rows),
                cap=MAX_TOOL_RESULT_ROWS,
            )
            masked_rows = masked_rows[:MAX_TOOL_RESULT_ROWS]

        return ToolResult(tool_use_id="", content=json.dumps(masked_rows, default=str))

    def _dispatch_aggregate_gold_table(self, tool_input: dict) -> ToolResult:
        """Same in-process path as `_dispatch_query_gold_table`, for the same
        reason. aggregate_gold_table already refuses masked columns as the
        metric, a filter, or a group key, so its rows can't carry PHI; masking
        still runs on them so that guarantee never rests on one check alone.
        A zero count is a real answer here (unlike an empty row list), so
        there is no groundedness refusal on this path."""
        kwargs = {}
        if self.duckdb_path is not None:
            kwargs["db_path"] = self.duckdb_path
        if self.seed_sql_path is not None:
            kwargs["seed_sql_path"] = self.seed_sql_path
        optional = ("column", "filters", "where", "group_by", "as_of")
        args = {key: tool_input[key] for key in optional if key in tool_input}
        table = tool_input.get("table", "")
        result = data_query.aggregate_gold_table(table, tool_input.get("metric", ""), **args, **kwargs)
        if result.is_error:
            return result
        payload = json.loads(result.content)
        payload["rows"] = governance_guard.enforce_masking(table, payload["rows"], role=ORCHESTRATOR_ROLE)
        return ToolResult(tool_use_id="", content=json.dumps(payload, default=str))

    def _dispatch_pipeline_health(self, tool_name: str, tool_input: dict) -> ToolResult:
        """Handles the read-only health checks and notify_and_page — the
        tools NOT gated by the kill switch or the escalation ceiling.
        quarantine_bad_records/restart_pipeline have their own dedicated
        `_dispatch_quarantine`/`_dispatch_restart` methods below.

        Routed through the MCP bridge (`mcp_server/server.py` exposes the
        exact same `agent.tools.pipeline_health` functions this used to call
        directly) rather than `getattr(pipeline_health, tool_name)(...)` —
        `state_path` is injected into the call here exactly as it always was,
        just as an explicit dict key instead of a positional argument, since
        the tool now crosses a real (if local, stdio-only) process boundary.
        """
        return self.mcp_bridge.dispatch(tool_name, {**tool_input, "state_path": str(self.state_path)})

    def _dispatch_quarantine(self, tool_input: dict) -> ToolResult:
        table = tool_input.get("table", "")
        expectation = tool_input.get("expectation", "")

        if not self._autonomous_remediation_enabled():
            return self._killswitch_refusal("quarantine_bad_records", tool_input)

        state = pipeline_health.load_state(self.state_path)
        attempts = state.get("remediation_attempts", {}).get(table, {}).get(expectation, 0)
        if attempts >= ESCALATION_CEILING:
            return self._force_escalation(table, expectation, attempts)

        quarantine_input = {"state_path": str(self.state_path), "table": table, "expectation": expectation}
        result = self.mcp_bridge.dispatch("quarantine_bad_records", quarantine_input)
        if not result.is_error:
            self._remediated = True
            # Re-read after quarantine_bad_records's own write so we're
            # incrementing on top of the latest state, not a stale copy.
            state = pipeline_health.load_state(self.state_path)
            remediation_attempts = state.setdefault("remediation_attempts", {})
            remediation_attempts.setdefault(table, {})[expectation] = attempts + 1
            pipeline_health.save_state(self.state_path, state)
        return result

    def _dispatch_restart(self, tool_input: dict) -> ToolResult:
        if not self._autonomous_remediation_enabled():
            return self._killswitch_refusal("restart_pipeline", tool_input)

        result = self.mcp_bridge.dispatch("restart_pipeline", {**tool_input, "state_path": str(self.state_path)})
        if not result.is_error:
            self._remediated = True
        return result

    def _autonomous_remediation_enabled(self) -> bool:
        """Kill-switch guardrail: reads `autonomous_remediation_enabled` from
        the state file, defaulting to True if absent (existing state files
        predating this flag keep working as "enabled").
        """
        state = pipeline_health.load_state(self.state_path)
        return bool(state.get("autonomous_remediation_enabled", True))

    def _killswitch_refusal(self, tool_name: str, tool_input: dict) -> ToolResult:
        message = (
            f"Kill switch active (autonomous_remediation_enabled=false): refused "
            f"{tool_name}({tool_input})."
        )
        notify_input = {"state_path": str(self.state_path), "message": message}
        escalation = self.mcp_bridge.dispatch("notify_and_page", notify_input)
        self._tool_calls.append("notify_and_page")
        logger.warning("kill_switch_refused_tool", tool=tool_name, tool_input=tool_input)
        payload = {
            "ok": False,
            "error": "autonomous_remediation_enabled is false; refusing destructive action",
            "tool": tool_name,
            "notify_result": json.loads(escalation.content),
        }
        return ToolResult(tool_use_id="", content=json.dumps(payload), is_error=True)

    def _force_escalation(self, table: str, expectation: str, attempts: int) -> ToolResult:
        message = (
            f"Escalation ceiling reached: {table}/{expectation} still failing after "
            f"{attempts} automated quarantine attempts. Escalating instead of retrying."
        )
        notify_input = {"state_path": str(self.state_path), "message": message}
        escalation = self.mcp_bridge.dispatch("notify_and_page", notify_input)
        self._tool_calls.append("notify_and_page")
        logger.warning("escalation_ceiling_reached", table=table, expectation=expectation, attempts=attempts)
        payload = {
            "ok": False,
            "escalated": True,
            "reason": "escalation_ceiling_reached",
            "table": table,
            "expectation": expectation,
            "notify_result": json.loads(escalation.content),
        }
        return ToolResult(tool_use_id="", content=json.dumps(payload), is_error=True)

    def _apply_output_size_guard(self, result: ToolResult) -> ToolResult:
        if len(result.content) <= MAX_TOOL_RESULT_CHARS:
            return result
        logger.warning(
            "truncating_tool_result_content", original_chars=len(result.content), cap=MAX_TOOL_RESULT_CHARS
        )
        truncated = result.content[:MAX_TOOL_RESULT_CHARS] + "...<truncated>"
        return ToolResult(tool_use_id=result.tool_use_id, content=truncated, is_error=result.is_error)

    def _append_audit_log(self, tool_name: str, tool_input: dict, result: ToolResult) -> None:
        """Immutable audit trail: one append-only JSONL line per dispatch()
        call, containing the ALREADY-MASKED tool output — never a raw
        pre-mask value, since `result` here is what's already survived
        `_dispatch`'s masking/injection-guard/size-guard pipeline. Opens with
        mode "a" specifically so normal operation can only ever grow this
        file, never rewrite or truncate it.
        """
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "mode": self._current_mode,
            "tool": tool_name,
            "input": tool_input,
            "output": result.content,
            "is_error": result.is_error,
        }
        self.audit_log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.audit_log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")

    @staticmethod
    def _apply_injection_guard(result: ToolResult) -> ToolResult:
        if governance_guard.scan_for_injection(result.content):
            wrapped = f"<untrusted_data>{result.content}</untrusted_data>"
            return ToolResult(tool_use_id=result.tool_use_id, content=wrapped, is_error=result.is_error)
        return result
