"""Critical test: proves prompt-injection defense is architecturally applied
to every tool result, not just documented in a system prompt. Part (a)
unit-tests governance_guard.scan_for_injection. Part (b) drives it through
OrchestratorAgent's dispatch wrapping and asserts the wrapped, not raw,
content is what would reach `messages`.
"""

import json

from agent.llm import ToolResult
from agent.orchestrator import OrchestratorAgent
from agent.tools import pipeline_health
from agent.tools.governance_guard import scan_for_injection
from simulator.chaos import build_prompt_injection_payload

BENIGN_NOTE = "Patient resting comfortably, vitals stable, no acute distress noted."


# --- (a) unit tests -----------------------------------------------------


def test_scan_for_injection_flags_the_chaos_payload():
    payload = build_prompt_injection_payload()
    assert scan_for_injection(payload["notes"]) is True


def test_scan_for_injection_allows_benign_note():
    assert scan_for_injection(BENIGN_NOTE) is False


def test_scan_for_injection_allows_empty_text():
    assert scan_for_injection("") is False


# --- (b) end-to-end via OrchestratorAgent._dispatch ----------------------


def test_dispatch_wraps_injected_tool_content_before_it_can_reach_messages(monkeypatch, tmp_path):
    injected_notes = build_prompt_injection_payload()["notes"]

    def fake_check(state_path):
        # Simulates a tool result whose content happens to carry an
        # untrusted note (e.g. bundled diagnostic context) containing an
        # injection attempt.
        return ToolResult(tool_use_id="", content=json.dumps({"ok": True, "note": injected_notes}))

    monkeypatch.setattr(pipeline_health, "check_expectation_metrics", fake_check)

    state_path = tmp_path / "pipeline_state.json"
    state_path.write_text(json.dumps({"tables": {}, "jobs": {}, "schema_snapshot": {}, "incidents": []}))

    orchestrator = OrchestratorAgent(
        claude=_NoopClaude(), state_path=state_path, audit_log_path=tmp_path / "audit_log.jsonl"
    )
    result = orchestrator._dispatch("check_expectation_metrics", {})

    assert result.content.startswith("<untrusted_data>")
    assert result.content.endswith("</untrusted_data>")
    assert injected_notes in result.content  # content preserved, just tagged as untrusted, not stripped


class _NoopClaude:
    def run_tool_loop(self, system, messages, tools, dispatch, max_turns=6):
        return "noop"
