"""Proves the escalation-ceiling guardrail is enforced in code: once a
(table, expectation) pair has already been "fixed" ESCALATION_CEILING times
and the failure is STILL present, the next quarantine attempt must be refused
and forced to notify_and_page instead of quarantining (again) — the model
cannot be trusted to self-limit a retry loop, so this has to hold even when a
scripted "misbehaving" fake Claude keeps asking to quarantine the same thing.
"""

import json
from pathlib import Path

from agent.orchestrator import ESCALATION_CEILING, OrchestratorAgent
from agent.tools import pipeline_health

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_STATE = REPO_ROOT / "data" / "state" / "pipeline_state.example.json"


def _fresh_state_path(tmp_path) -> Path:
    state_path = tmp_path / "pipeline_state.json"
    state_path.write_text(EXAMPLE_STATE.read_text())
    return state_path


class ScriptedSequenceClaude:
    def __init__(self, steps, final_answer="Done."):
        self.steps = steps
        self.final_answer = final_answer

    def run_tool_loop(self, system, messages, tools, dispatch, max_turns=6):
        for tool_name, tool_input in self.steps:
            dispatch(tool_name, tool_input)
        return self.final_answer


def test_quarantine_attempts_below_ceiling_are_allowed(tmp_path):
    state_path = _fresh_state_path(tmp_path)
    pipeline_health.quarantine_bad_records(state_path, "silver_vitals", "plausible_vital_value")
    # Simulate the failure recurring but only ESCALATION_CEILING - 1 attempts so far.
    state = pipeline_health.load_state(state_path)
    state["tables"]["silver_vitals"]["expectations"]["plausible_vital_value"]["failure_count"] = 5
    state["remediation_attempts"] = {"silver_vitals": {"plausible_vital_value": ESCALATION_CEILING - 1}}
    pipeline_health.save_state(state_path, state)

    orchestrator = OrchestratorAgent(
        claude=ScriptedSequenceClaude([]), state_path=state_path, audit_log_path=tmp_path / "audit_log.jsonl"
    )
    result = orchestrator._dispatch(
        "quarantine_bad_records", {"table": "silver_vitals", "expectation": "plausible_vital_value"}
    )

    assert result.is_error is False
    final_state = pipeline_health.load_state(state_path)
    assert final_state["tables"]["silver_vitals"]["expectations"]["plausible_vital_value"]["failure_count"] == 0
    assert final_state["remediation_attempts"]["silver_vitals"]["plausible_vital_value"] == ESCALATION_CEILING
    assert "notify_and_page" not in orchestrator._tool_calls


def test_ceiling_reached_forces_escalation_instead_of_quarantining_again(tmp_path):
    state_path = _fresh_state_path(tmp_path)
    state = pipeline_health.load_state(state_path)
    # Same failure has already been "fixed" ESCALATION_CEILING times, but it's
    # still failing right now — the 4th ask must not quarantine again.
    state["tables"]["silver_vitals"]["expectations"]["plausible_vital_value"]["failure_count"] = 7
    state["remediation_attempts"] = {"silver_vitals": {"plausible_vital_value": ESCALATION_CEILING}}
    pipeline_health.save_state(state_path, state)

    steps = [
        ("check_expectation_metrics", {}),
        ("quarantine_bad_records", {"table": "silver_vitals", "expectation": "plausible_vital_value"}),
    ]
    claude = ScriptedSequenceClaude(steps, final_answer="Escalated after repeated failures.")
    orchestrator = OrchestratorAgent(
        claude=claude, state_path=state_path, audit_log_path=tmp_path / "audit_log.jsonl"
    )

    result = orchestrator.handle("Check pipeline health and fix anything auto-fixable.", mode="de")

    # The forced escalation happened instead of a real quarantine.
    assert "notify_and_page" in result.tool_calls
    assert result.remediated is False

    final_state = pipeline_health.load_state(state_path)
    # Failure count is untouched — no quarantine was actually performed.
    assert final_state["tables"]["silver_vitals"]["expectations"]["plausible_vital_value"]["failure_count"] == 7
    # Escalation-ceiling counter did not increment further (no new attempt happened).
    assert final_state["remediation_attempts"]["silver_vitals"]["plausible_vital_value"] == ESCALATION_CEILING
    # A real incident was filed via notify_and_page.
    assert len(final_state["incidents"]) == 1
    assert "escalation ceiling" in final_state["incidents"][0]["message"].lower()


def test_forced_escalation_result_is_marked_as_error(tmp_path):
    state_path = _fresh_state_path(tmp_path)
    state = pipeline_health.load_state(state_path)
    state["remediation_attempts"] = {"silver_vitals": {"plausible_vital_value": ESCALATION_CEILING}}
    pipeline_health.save_state(state_path, state)

    orchestrator = OrchestratorAgent(
        claude=ScriptedSequenceClaude([]), state_path=state_path, audit_log_path=tmp_path / "audit_log.jsonl"
    )
    result = orchestrator._dispatch(
        "quarantine_bad_records", {"table": "silver_vitals", "expectation": "plausible_vital_value"}
    )

    assert result.is_error is True
    payload = json.loads(result.content)
    assert payload["escalated"] is True
    assert payload["reason"] == "escalation_ceiling_reached"
