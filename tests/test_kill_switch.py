"""Proves the kill-switch guardrail is enforced in the dispatch wrapper, in
code: with `autonomous_remediation_enabled=false` in the state file,
quarantine_bad_records/restart_pipeline must be refused (an error ToolResult,
not a silent allow), while the read-only health-check tools keep working.
"""

import json
from pathlib import Path

from agent.orchestrator import OrchestratorAgent
from agent.tools import pipeline_health

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_STATE = REPO_ROOT / "data" / "state" / "pipeline_state.example.json"


def _state_path_with_kill_switch(tmp_path, enabled: bool) -> Path:
    state_path = tmp_path / "pipeline_state.json"
    state = json.loads(EXAMPLE_STATE.read_text())
    state["tables"]["silver_vitals"]["expectations"]["plausible_vital_value"]["failure_count"] = 5
    state["autonomous_remediation_enabled"] = enabled
    state_path.write_text(json.dumps(state))
    return state_path


class _NoopClaude:
    def run_tool_loop(self, system, messages, tools, dispatch, max_turns=6):
        return "noop"


def _orchestrator(state_path, tmp_path) -> OrchestratorAgent:
    return OrchestratorAgent(
        claude=_NoopClaude(), state_path=state_path, audit_log_path=tmp_path / "audit_log.jsonl"
    )


def test_quarantine_is_refused_when_kill_switch_disabled(tmp_path):
    state_path = _state_path_with_kill_switch(tmp_path, enabled=False)
    orchestrator = _orchestrator(state_path, tmp_path)

    result = orchestrator._dispatch(
        "quarantine_bad_records", {"table": "silver_vitals", "expectation": "plausible_vital_value"}
    )

    assert result.is_error is True
    final_state = pipeline_health.load_state(state_path)
    # Untouched — refused before any quarantine happened.
    assert final_state["tables"]["silver_vitals"]["expectations"]["plausible_vital_value"]["failure_count"] == 5
    assert "notify_and_page" in orchestrator._tool_calls
    assert len(final_state["incidents"]) == 1


def test_restart_pipeline_is_refused_when_kill_switch_disabled(tmp_path):
    state_path = _state_path_with_kill_switch(tmp_path, enabled=False)
    orchestrator = _orchestrator(state_path, tmp_path)

    result = orchestrator._dispatch("restart_pipeline", {"pipeline_name": "streaming_patient_pipeline"})

    assert result.is_error is True
    final_state = pipeline_health.load_state(state_path)
    assert final_state["jobs"]["streaming_patient_pipeline"]["status"] == "RUNNING"  # unchanged
    assert "notify_and_page" in orchestrator._tool_calls


def test_read_only_health_checks_still_work_when_kill_switch_disabled(tmp_path):
    state_path = _state_path_with_kill_switch(tmp_path, enabled=False)
    orchestrator = _orchestrator(state_path, tmp_path)

    metrics_result = orchestrator._dispatch("check_expectation_metrics", {})
    job_result = orchestrator._dispatch("check_job_status", {})
    drift_result = orchestrator._dispatch("detect_schema_drift", {})

    assert metrics_result.is_error is False
    assert job_result.is_error is False
    assert drift_result.is_error is False
    payload = json.loads(metrics_result.content)
    assert payload["failing_expectations"][0]["table"] == "silver_vitals"


def test_notify_and_page_still_works_when_kill_switch_disabled(tmp_path):
    state_path = _state_path_with_kill_switch(tmp_path, enabled=False)
    orchestrator = _orchestrator(state_path, tmp_path)

    result = orchestrator._dispatch("notify_and_page", {"message": "manual escalation"})

    assert result.is_error is False
    final_state = pipeline_health.load_state(state_path)
    assert any("manual escalation" in incident["message"] for incident in final_state["incidents"])


def test_quarantine_succeeds_when_kill_switch_enabled(tmp_path):
    state_path = _state_path_with_kill_switch(tmp_path, enabled=True)
    orchestrator = _orchestrator(state_path, tmp_path)

    result = orchestrator._dispatch(
        "quarantine_bad_records", {"table": "silver_vitals", "expectation": "plausible_vital_value"}
    )

    assert result.is_error is False
    final_state = pipeline_health.load_state(state_path)
    assert final_state["tables"]["silver_vitals"]["expectations"]["plausible_vital_value"]["failure_count"] == 0


def test_kill_switch_defaults_to_enabled_when_absent(tmp_path):
    # The example state file (pre-guardrails) does not set the flag at all —
    # default must be "enabled" so pre-existing state files keep working.
    state_path = tmp_path / "pipeline_state.json"
    state = json.loads(EXAMPLE_STATE.read_text())
    state.pop("autonomous_remediation_enabled", None)
    state_path.write_text(json.dumps(state))

    orchestrator = _orchestrator(state_path, tmp_path)
    assert orchestrator._autonomous_remediation_enabled() is True
