"""Critical test: proves the orchestrator agent distinguishes auto-fixable
data-quality failures (quarantine + restart, no human paged) from hard-stop
contract breaks (escalate via notify_and_page, no attempted auto-fix).

Both cases use a deterministic scripted fake Claude that calls a fixed
sequence of tools via dispatch — no real LLM call, no randomness in which
tools get invoked.
"""

import json
from pathlib import Path

from agent.orchestrator import OrchestratorAgent
from simulator.chaos import simulate_pipeline_failure

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_STATE = REPO_ROOT / "data" / "state" / "pipeline_state.example.json"


class ScriptedSequenceClaude:
    """Deterministic fake Claude: dispatches a fixed sequence of
    (tool_name, tool_input) pairs, in order, then returns a final answer.
    Same run_tool_loop signature as agent.llm.Claude; makes zero network calls.
    """

    def __init__(self, steps, final_answer="Done."):
        self.steps = steps
        self.final_answer = final_answer

    def run_tool_loop(self, system, messages, tools, dispatch, max_turns=6):
        for tool_name, tool_input in self.steps:
            dispatch(tool_name, tool_input)
        return self.final_answer


def _fresh_state_path(tmp_path) -> Path:
    state_path = tmp_path / "pipeline_state.json"
    state_path.write_text(EXAMPLE_STATE.read_text())
    return state_path


def test_auto_fixable_failure_is_remediated_without_paging(tmp_path):
    state_path = _fresh_state_path(tmp_path)
    simulate_pipeline_failure(state_path, "silver_vitals", "plausible_vital_value", failure_count=5)

    steps = [
        ("check_expectation_metrics", {}),
        ("quarantine_bad_records", {"table": "silver_vitals", "expectation": "plausible_vital_value"}),
        ("restart_pipeline", {"pipeline_name": "streaming_patient_pipeline"}),
        ("check_expectation_metrics", {}),
    ]
    claude = ScriptedSequenceClaude(
        steps, final_answer="Cleared the plausible_vital_value failure and restarted the pipeline."
    )
    audit_log_path = tmp_path / "audit_log.jsonl"
    orchestrator = OrchestratorAgent(claude=claude, state_path=state_path, audit_log_path=audit_log_path)

    result = orchestrator.handle("Check pipeline health and fix anything auto-fixable.", mode="de")

    final_state = json.loads(state_path.read_text())
    assert final_state["tables"]["silver_vitals"]["expectations"]["plausible_vital_value"]["failure_count"] == 0
    # notify_and_page's real effect (an appended incident) is checked directly
    # against the shared state file rather than via a monkeypatch spy on
    # agent.tools.pipeline_health.notify_and_page — that function now runs
    # inside the MCP server subprocess (see agent/mcp_bridge.py), a separate
    # process a monkeypatch in this test process cannot reach.
    assert final_state["incidents"] == []
    assert result.remediated is True
    assert result.mode == "de"
    assert "quarantine_bad_records" in result.tool_calls
    assert "restart_pipeline" in result.tool_calls
    assert "notify_and_page" not in result.tool_calls


def test_hard_stop_failure_is_escalated_not_auto_fixed(tmp_path):
    state_path = _fresh_state_path(tmp_path)
    simulate_pipeline_failure(state_path, "silver_encounters", "known_event_type", failure_count=1)

    steps = [
        ("check_expectation_metrics", {}),
        (
            "notify_and_page",
            {"message": "Hard-stop failure: known_event_type contract break in silver_encounters."},
        ),
    ]
    claude = ScriptedSequenceClaude(steps, final_answer="Escalated the hard-stop contract break to on-call.")
    audit_log_path = tmp_path / "audit_log.jsonl"
    orchestrator = OrchestratorAgent(claude=claude, state_path=state_path, audit_log_path=audit_log_path)

    result = orchestrator.handle("Check pipeline health.", mode="de")

    final_state = json.loads(state_path.read_text())
    # notify_and_page's real effect (an appended incident) is checked
    # directly against the shared state file — see the comment in
    # test_auto_fixable_failure_is_remediated_without_paging above for why a
    # monkeypatch spy no longer works here.
    assert len(final_state["incidents"]) == 1
    assert "known_event_type" in final_state["incidents"][0]["message"]
    # Left untouched — no quarantine/restart was attempted on a hard-stop failure.
    assert final_state["tables"]["silver_encounters"]["expectations"]["known_event_type"]["failure_count"] == 1
    assert "notify_and_page" in result.tool_calls
    assert "quarantine_bad_records" not in result.tool_calls
    assert result.remediated is False
