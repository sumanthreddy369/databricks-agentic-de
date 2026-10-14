"""Proves earned autonomy (agent/autonomy.py): an action class runs without
approval only after a run of approved, successful executions AND a named
person's promotion; it's demoted automatically on its first failure; and
non-remediation tools can never be promoted. Zero network calls."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from agent import approvals, autonomy
from agent.orchestrator import OrchestratorAgent
from agent.tools import pipeline_health

CLS = "quarantine_bad_records:silver_vitals:plausible_vital_value"
INPUT = {"table": "silver_vitals", "expectation": "plausible_vital_value"}


class _Tools:
    """Fake MCP bridge running the real pipeline_health functions; can be told to fail."""

    def __init__(self):
        self.fail = False

    def dispatch(self, tool_name, tool_input):
        args = dict(tool_input)
        state_path = args.pop("state_path")
        if self.fail and tool_name == "quarantine_bad_records":
            args["table"] = "no_such_table"  # makes the real tool return an error
        return getattr(pipeline_health, tool_name)(state_path, **args)


class _NoopClaude:
    def run_tool_loop(self, *a, **k):
        return "noop"


def _history(statuses, start=datetime(2026, 10, 1, tzinfo=UTC)):
    out = []
    for i, status in enumerate(statuses):
        t = start + timedelta(hours=i)
        out.append(
            {
                "id": f"apr_{i}",
                "tool": "quarantine_bad_records",
                "input": INPUT,
                "status": status,
                "requested_at": t.isoformat(),
                "decided_at": (t + timedelta(minutes=5)).isoformat(),
            }
        )
    return out


@pytest.fixture
def state_path(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "tables": {"silver_vitals": {"expectations": {"plausible_vital_value": {"failure_count": 7}}}},
                "jobs": {},
                "incidents": [],
                "pending_approvals": _history(["executed"] * autonomy.PROMOTION_STREAK),
            }
        )
    )
    return path


def _state(path):
    return json.loads(path.read_text())


def test_streak_counts_only_the_latest_unbroken_run():
    state = {"pending_approvals": _history(["executed", "rejected", "executed", "expired", "executed"])}
    # newest first: executed, (expired skipped), executed, then rejected stops it
    assert autonomy.streak(state, CLS) == 2


def test_not_eligible_before_the_streak():
    state = {"pending_approvals": _history(["executed"] * (autonomy.PROMOTION_STREAK - 1))}
    ok, reason = autonomy.eligibility(state, CLS)
    assert not ok and f"{autonomy.PROMOTION_STREAK - 1} of {autonomy.PROMOTION_STREAK}" in reason
    with pytest.raises(ValueError, match="not eligible"):
        autonomy.promote(state, CLS, "Jane Doe")


def test_a_rejection_resets_eligibility():
    state = {"pending_approvals": _history(["executed"] * autonomy.PROMOTION_STREAK + ["rejected"])}
    assert autonomy.eligibility(state, CLS)[0] is False


def test_unpromotable_tools_never_qualify():
    state = {"pending_approvals": [{**a, "tool": "notify_and_page"} for a in _history(["executed"] * 20)]}
    ok, reason = autonomy.eligibility(state, "notify_and_page")
    assert not ok and "never" in reason


def test_promotion_needs_a_named_person():
    state = {"pending_approvals": _history(["executed"] * autonomy.PROMOTION_STREAK)}
    with pytest.raises(ValueError, match="name"):
        autonomy.promote(state, CLS, " ")


def test_eligible_but_unpromoted_still_needs_approval(state_path, tmp_path):
    agent = OrchestratorAgent(
        claude=_NoopClaude(), state_path=state_path, audit_log_path=tmp_path / "a.jsonl", mcp_bridge=_Tools()
    )
    payload = json.loads(agent._dispatch("quarantine_bad_records", INPUT).content)
    assert payload["pending_approval"] is True


def test_promoted_class_runs_without_approval(state_path, tmp_path, capsys):
    common = ["--state-path", str(state_path), "--audit-log-path", str(tmp_path / "a.jsonl")]
    assert approvals.run([*common, "promote", CLS, "--by", "Jane Doe"]) == 0
    agent = OrchestratorAgent(
        claude=_NoopClaude(), state_path=state_path, audit_log_path=tmp_path / "a.jsonl", mcp_bridge=_Tools()
    )

    result = agent._dispatch("quarantine_bad_records", INPUT)

    assert result.is_error is False
    assert (
        _state(state_path)["tables"]["silver_vitals"]["expectations"]["plausible_vital_value"]["failure_count"]
        == 0
    )
    assert _state(state_path)["autonomy"]["classes"][CLS]["promoted_by"] == "Jane Doe"


def test_first_failure_demotes_automatically(state_path, tmp_path):
    state = _state(state_path)
    autonomy.promote(state, CLS, "Jane Doe")
    state_path.write_text(json.dumps(state))
    tools = _Tools()
    tools.fail = True
    agent = OrchestratorAgent(
        claude=_NoopClaude(), state_path=state_path, audit_log_path=tmp_path / "a.jsonl", mcp_bridge=tools
    )

    assert agent._dispatch("quarantine_bad_records", INPUT).is_error is True
    entry = _state(state_path)["autonomy"]["classes"][CLS]
    assert entry["policy"] == "approval"
    assert "failed" in entry["demoted_because"]
    # next request goes back to needing a person
    tools.fail = False
    assert json.loads(agent._dispatch("quarantine_bad_records", INPUT).content)["pending_approval"] is True


def test_kill_switch_still_beats_earned_autonomy(state_path, tmp_path):
    state = _state(state_path)
    autonomy.promote(state, CLS, "Jane Doe")
    state["autonomous_remediation_enabled"] = False
    state_path.write_text(json.dumps(state))
    agent = OrchestratorAgent(
        claude=_NoopClaude(), state_path=state_path, audit_log_path=tmp_path / "a.jsonl", mcp_bridge=_Tools()
    )

    assert agent._dispatch("quarantine_bad_records", INPUT).is_error is True
    assert (
        _state(state_path)["tables"]["silver_vitals"]["expectations"]["plausible_vital_value"]["failure_count"]
        == 7
    )


def test_classes_command_lists_progress(state_path, tmp_path, capsys):
    assert approvals.run(["--state-path", str(state_path), "classes"]) == 0
    [row] = json.loads(capsys.readouterr().out)
    assert row == {
        "class": CLS,
        "policy": "approval",
        "streak": autonomy.PROMOTION_STREAK,
        "eligible": True,
        "detail": f"{autonomy.PROMOTION_STREAK} consecutive approved, successful executions",
    }
