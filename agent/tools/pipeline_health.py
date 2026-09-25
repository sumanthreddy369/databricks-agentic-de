"""DE-mode tools: pipeline health checks and remediation actions.

Every function reads and writes a local JSON state file (path injected via
`state_path`) instead of calling a live Databricks workspace, so the whole
DE-mode loop is testable without a cluster. Each docstring names the real
Databricks REST API the function stands in for — swapping these for real API
calls is the only change needed to run this against a live workspace.

State file shape (see data/state/pipeline_state.example.json):
{
  "tables": {"<table>": {"expectations": {"<name>": {"failure_count": int}}}},
  "jobs": {"<job_or_pipeline_name>": {"status": "RUNNING"|"SUCCESS"|"FAILED"}},
  "schema_snapshot": {"patient_events": ["event_id", ...]},
  "incidents": [{"message": str, "ts": str}, ...],
  "autonomous_remediation_enabled": bool,   # kill switch, default true if absent
  "remediation_attempts": {"<table>": {"<expectation>": int}}  # escalation-ceiling counters
}

The last two keys are read/written by agent/orchestrator.py's guardrail
wiring (`_autonomous_remediation_enabled`, `_dispatch_quarantine`), not by
any function in this file — they're documented here because they live in the
same state file this module owns.
"""

import json
from pathlib import Path

from agent.llm import ToolResult
from common.contracts import PATIENT_EVENT_ENVELOPE_FIELDS


def _load(state_path: Path) -> dict:
    return json.loads(Path(state_path).read_text())


def _save(state_path: Path, state: dict) -> None:
    Path(state_path).write_text(json.dumps(state, indent=2))


# Public aliases: agent/orchestrator.py reads/writes this same state file for
# its own guardrail bookkeeping (kill switch, escalation-ceiling counters)
# and should go through named functions rather than reaching into this
# module's leading-underscore internals.
load_state = _load
save_state = _save


def check_expectation_metrics(state_path: Path) -> ToolResult:
    """Real equivalent: DLT pipeline event log API
    (`GET /api/2.0/pipelines/{pipeline_id}/events`, filtered to
    `flow_progress` events' `data_quality.expectations[]`).

    Flags any expectation whose failure_count is > 0.
    """
    state = _load(state_path)
    failing = []
    for table, table_state in state.get("tables", {}).items():
        for expectation, metrics in table_state.get("expectations", {}).items():
            if metrics.get("failure_count", 0) > 0:
                failing.append(
                    {"table": table, "expectation": expectation, "failure_count": metrics["failure_count"]}
                )
    payload = {"ok": not failing, "failing_expectations": failing}
    return ToolResult(tool_use_id="", content=json.dumps(payload))


def check_job_status(state_path: Path) -> ToolResult:
    """Real equivalent: Jobs API `GET /api/2.1/jobs/runs/get` (or the
    Pipelines API's own status field for a DLT pipeline)."""
    state = _load(state_path)
    jobs = state.get("jobs", {})
    unhealthy = {
        name: info["status"] for name, info in jobs.items() if info.get("status") not in ("RUNNING", "SUCCESS")
    }
    payload = {"ok": not unhealthy, "jobs": jobs, "unhealthy": unhealthy}
    return ToolResult(tool_use_id="", content=json.dumps(payload))


def detect_schema_drift(state_path: Path) -> ToolResult:
    """Real equivalent: comparing the live Kafka topic schema (e.g. via a
    schema registry, or sampling recent messages) against the contract in
    common.contracts.PATIENT_EVENT_ENVELOPE_FIELDS."""
    state = _load(state_path)
    observed = state.get("schema_snapshot", {}).get("patient_events", [])
    expected = list(PATIENT_EVENT_ENVELOPE_FIELDS)
    missing = [f for f in expected if f not in observed]
    unexpected = [f for f in observed if f not in expected]
    drifted = bool(missing or unexpected)
    payload = {"ok": not drifted, "missing_fields": missing, "unexpected_fields": unexpected}
    return ToolResult(tool_use_id="", content=json.dumps(payload))


def quarantine_bad_records(state_path: Path, table: str, expectation: str) -> ToolResult:
    """Real equivalent: re-running the DLT pipeline update with the offending
    batch's bad records routed to a quarantine table (or simply clearing the
    condition upstream and letting the expectation re-evaluate clean)."""
    state = _load(state_path)
    try:
        state["tables"][table]["expectations"][expectation]["failure_count"] = 0
    except KeyError:
        return ToolResult(
            tool_use_id="",
            content=json.dumps({"ok": False, "error": f"unknown table/expectation: {table}/{expectation}"}),
            is_error=True,
        )
    _save(state_path, state)
    payload = {"ok": True, "table": table, "expectation": expectation}
    return ToolResult(tool_use_id="", content=json.dumps(payload))


def restart_pipeline(state_path: Path, pipeline_name: str) -> ToolResult:
    """Real equivalent: Pipelines API `POST /api/2.0/pipelines/{pipeline_id}/updates`
    (or Jobs API `runs/submit`/`runs/repair` for a job-based pipeline)."""
    state = _load(state_path)
    if pipeline_name not in state.get("jobs", {}):
        error_payload = {"ok": False, "error": f"unknown pipeline: {pipeline_name}"}
        return ToolResult(tool_use_id="", content=json.dumps(error_payload), is_error=True)
    state["jobs"][pipeline_name]["status"] = "RUNNING"
    _save(state_path, state)
    # Simulated instantaneous successful restart for local testability.
    state["jobs"][pipeline_name]["status"] = "SUCCESS"
    _save(state_path, state)
    payload = {"ok": True, "pipeline": pipeline_name, "status": "SUCCESS"}
    return ToolResult(tool_use_id="", content=json.dumps(payload))


def notify_and_page(state_path: Path, message: str) -> ToolResult:
    """Real equivalent: an incident-management/paging integration (e.g.
    PagerDuty Events API, Slack webhook). This is the escalation-only tool —
    used when a check fails and no remediation tool applies, most notably a
    hard-stop expectation failure like `known_event_type`."""
    state = _load(state_path)
    incidents = state.setdefault("incidents", [])
    incidents.append({"message": message})
    _save(state_path, state)
    return ToolResult(tool_use_id="", content=json.dumps({"ok": True, "paged": True, "message": message}))
