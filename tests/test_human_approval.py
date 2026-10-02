"""Proves the human-approval guardrail: with remediation_approval "required"
(the default whenever the key is absent), quarantine_bad_records and
restart_pipeline only queue a request; nothing changes until a person
approves it through agent/approvals.py, and the approved action still runs
through the kill switch, escalation ceiling, and audit log. Zero network
calls (a fake MCP bridge stands in for the tool server).
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from agent import approvals, healthcheck
from agent.llm import ToolResult
from agent.orchestrator import DA_TOOLS, DE_TOOLS, ESCALATION_CEILING, OrchestratorAgent
from agent.tools import pipeline_health


class _ToolsInProcess:
    """Fake MCP bridge: runs the real pipeline_health functions in-process."""

    def dispatch(self, tool_name, tool_input):
        args = dict(tool_input)
        state_path = args.pop("state_path")
        return getattr(pipeline_health, tool_name)(state_path, **args)


class _ScriptedClaude:
    def __init__(self, steps):
        self.steps = steps

    def run_tool_loop(self, system, messages, tools, dispatch, max_turns=6):
        for name, tool_input in self.steps:
            dispatch(name, tool_input)
        return "done"


QUARANTINE = ("quarantine_bad_records", {"table": "silver_vitals", "expectation": "plausible_vital_value"})


@pytest.fixture
def state_path(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "tables": {"silver_vitals": {"expectations": {"plausible_vital_value": {"failure_count": 7}}}},
                "jobs": {"streaming_patient_pipeline": {"status": "FAILED"}},
                "incidents": [],
            }
        )
    )
    return path


def _agent(state_path, tmp_path, claude=None):
    return OrchestratorAgent(
        claude=claude or _ScriptedClaude([]),
        state_path=state_path,
        audit_log_path=tmp_path / "audit.jsonl",
        mcp_bridge=_ToolsInProcess(),
    )


def _state(state_path):
    return json.loads(state_path.read_text())


def _failures(state_path):
    return _state(state_path)["tables"]["silver_vitals"]["expectations"]["plausible_vital_value"]["failure_count"]


def test_remediation_is_queued_not_executed_by_default(state_path, tmp_path):
    result = _agent(state_path, tmp_path, _ScriptedClaude([QUARANTINE])).handle("health check", mode="de")

    assert _failures(state_path) == 7  # nothing changed
    [request] = _state(state_path)["pending_approvals"]
    assert request["status"] == "pending"
    assert request["tool"] == "quarantine_bad_records"
    assert result.pending_approvals == [request["id"]]
    assert result.remediated is False
    assert "remediation_attempts" not in _state(state_path)


def test_restart_is_queued_too(state_path, tmp_path):
    agent = _agent(state_path, tmp_path)
    payload = json.loads(
        agent._dispatch("restart_pipeline", {"pipeline_name": "streaming_patient_pipeline"}).content
    )

    assert payload["pending_approval"] is True
    assert _state(state_path)["jobs"]["streaming_patient_pipeline"]["status"] == "FAILED"


def test_repeated_requests_reuse_the_pending_one(state_path, tmp_path):
    agent = _agent(state_path, tmp_path)
    first = json.loads(agent._dispatch(*QUARANTINE).content)["approval_id"]
    second = json.loads(agent._dispatch(*QUARANTINE).content)["approval_id"]

    assert first == second
    assert len(_state(state_path)["pending_approvals"]) == 1


def test_approval_executes_with_guardrails_and_records_the_approver(state_path, tmp_path):
    agent = _agent(state_path, tmp_path)
    approval_id = json.loads(agent._dispatch(*QUARANTINE).content)["approval_id"]

    result = agent.execute_approved(approval_id, "Jane Doe")

    assert result.is_error is False
    assert _failures(state_path) == 0
    assert _state(state_path)["remediation_attempts"]["silver_vitals"]["plausible_vital_value"] == 1
    [request] = _state(state_path)["pending_approvals"]
    assert (request["status"], request["decided_by"]) == ("executed", "Jane Doe")
    last = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[-1])
    assert (last["tool"], last["approved_by"], last["mode"]) == ("quarantine_bad_records", "Jane Doe", "approval")


def test_kill_switch_still_applies_at_approval_time(state_path, tmp_path):
    agent = _agent(state_path, tmp_path)
    approval_id = json.loads(agent._dispatch(*QUARANTINE).content)["approval_id"]
    state = _state(state_path)
    state["autonomous_remediation_enabled"] = False
    state_path.write_text(json.dumps(state))

    result = agent.execute_approved(approval_id, "Jane Doe")

    assert result.is_error is True
    assert _failures(state_path) == 7
    assert _state(state_path)["pending_approvals"][0]["status"] == "failed"


def test_escalation_ceiling_still_applies_at_approval_time(state_path, tmp_path):
    agent = _agent(state_path, tmp_path)
    approval_id = json.loads(agent._dispatch(*QUARANTINE).content)["approval_id"]
    state = _state(state_path)
    state["remediation_attempts"] = {"silver_vitals": {"plausible_vital_value": ESCALATION_CEILING}}
    state_path.write_text(json.dumps(state))

    result = agent.execute_approved(approval_id, "Jane Doe")

    assert json.loads(result.content)["escalated"] is True
    assert _failures(state_path) == 7


def test_expired_requests_cannot_be_approved(state_path, tmp_path):
    agent = _agent(state_path, tmp_path)
    approval_id = json.loads(agent._dispatch(*QUARANTINE).content)["approval_id"]
    state = _state(state_path)
    state["pending_approvals"][0]["expires_at"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    state_path.write_text(json.dumps(state))

    result = agent.execute_approved(approval_id, "Jane Doe")

    assert result.is_error is True
    assert "expired" in json.loads(result.content)["error"]
    assert _failures(state_path) == 7


def test_rejection_changes_nothing_and_is_audited(state_path, tmp_path):
    agent = _agent(state_path, tmp_path)
    approval_id = json.loads(agent._dispatch(*QUARANTINE).content)["approval_id"]

    assert agent.reject_approval(approval_id, "Jane Doe", "known upstream issue").is_error is False
    assert _failures(state_path) == 7
    assert _state(state_path)["pending_approvals"][0]["status"] == "rejected"
    assert agent.execute_approved(approval_id, "Jane Doe").is_error is True  # no longer pending
    last = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[-1])
    assert last["tool"] == "approval_rejected"


def test_an_approver_name_is_required(state_path, tmp_path):
    agent = _agent(state_path, tmp_path)
    approval_id = json.loads(agent._dispatch(*QUARANTINE).content)["approval_id"]

    assert agent.execute_approved(approval_id, "  ").is_error is True
    assert _failures(state_path) == 7


def test_auto_mode_keeps_the_old_behaviour(state_path, tmp_path):
    state = _state(state_path)
    state["remediation_approval"] = "auto"
    state_path.write_text(json.dumps(state))

    result = _agent(state_path, tmp_path)._dispatch(*QUARANTINE)

    assert result.is_error is False
    assert _failures(state_path) == 0
    assert "pending_approvals" not in _state(state_path)


def test_the_model_is_never_offered_an_approval_tool():
    names = {t["name"] for t in DE_TOOLS + DA_TOOLS}
    assert not {n for n in names if "approv" in n or "reject" in n}


def test_healthcheck_exits_3_when_a_remediation_awaits_approval(state_path, tmp_path, capsys):
    agent = _agent(state_path, tmp_path, _ScriptedClaude([QUARANTINE]))

    code = healthcheck.run(["--state-path", str(state_path)], agent=agent)

    assert code == healthcheck.EXIT_APPROVAL_PENDING


def test_cli_lists_and_approves(state_path, tmp_path, capsys):
    approval_id = json.loads(_agent(state_path, tmp_path)._dispatch(*QUARANTINE).content)["approval_id"]
    common = ["--state-path", str(state_path), "--audit-log-path", str(tmp_path / "audit.jsonl")]

    assert approvals.run([*common, "list"]) == 0
    assert approval_id in capsys.readouterr().out
    assert approvals.run([*common, "approve", approval_id, "--by", "Jane Doe"], mcp_bridge=_ToolsInProcess()) == 0
    assert _failures(state_path) == 0
    assert approvals.pending(state_path) == []


def test_tool_result_type_is_unchanged_for_queued_requests(state_path, tmp_path):
    result = _agent(state_path, tmp_path)._dispatch(*QUARANTINE)
    assert isinstance(result, ToolResult) and result.is_error is False
