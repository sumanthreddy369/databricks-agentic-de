"""Proves the human-work report (agent/human_work.py) counts what it claims,
from a synthetic audit log and approval list, and that it works end to end on
an audit log the orchestrator actually wrote."""

import json
from datetime import UTC, datetime, timedelta

from agent import human_work
from agent.llm import ToolResult
from agent.orchestrator import OrchestratorAgent

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


def line(minutes_ago, request_id, mode, tool, output):
    return {
        "ts": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
        "request_id": request_id,
        "mode": mode,
        "tool": tool,
        "input": {},
        "output": json.dumps(output) if not isinstance(output, str) else output,
        "is_error": False,
    }


AUDIT = [
    # DA question answered by Genie, nothing needed a person
    line(
        50, "r1", "da", "ask_genie", {"ok": True, "rows": [{"value": 34}], "guardrails": {"suppressed_cells": 0}}
    ),
    # DA question refused (identifier request)
    line(45, "r2", "da", "ask_genie", {"ok": False, "refused": True}),
    # DA question answered by the metric path with a suppressed group
    line(
        40, "r3", "da", "compute_metric", {"rows": [{"value": None, "suppressed": True}], "suppressed_groups": 1}
    ),
    # DE health check, all fine
    line(30, "r4", "de", "check_expectation_metrics", {"ok": True}),
    # DE health check: hard stop -> escalated
    line(20, "r5", "de", "lookup_problem", {"matches": [{"id": "B2"}]}),
    line(20, "r5", "de", "notify_and_page", {"ok": True, "paged": True}),
    # DE health check: fix requested, waiting for approval
    line(10, "r6", "de", "lookup_problem", {"matches": [{"id": "B1"}]}),
    line(
        10, "r6", "de", "quarantine_bad_records", {"ok": False, "pending_approval": True, "approval_id": "apr_1"}
    ),
    # outside the window
    line(60 * 24 * 10, "old", "da", "ask_genie", {"ok": True}),
    # the approval execution itself is not a new request
    line(5, "apr_1", "approval", "quarantine_bad_records", {"ok": True}),
]
APPROVALS = [
    {
        "id": "apr_1",
        "status": "executed",
        "requested_at": (NOW - timedelta(minutes=10)).isoformat(),
        "decided_at": (NOW - timedelta(minutes=5)).isoformat(),
        "decided_by": "Jane Doe",
    },
    {
        "id": "apr_2",
        "status": "rejected",
        "requested_at": (NOW - timedelta(minutes=90)).isoformat(),
        "decided_at": (NOW - timedelta(minutes=75)).isoformat(),
        "decided_by": "Sam Lee",
    },
    {"id": "apr_3", "status": "expired", "requested_at": (NOW - timedelta(minutes=300)).isoformat()},
]


def report():
    return human_work.build(AUDIT, APPROVALS, NOW - timedelta(days=7), NOW)


def test_counts_requests_not_tool_calls():
    r = report()
    assert r["requests"] == {"total": 6, "by_mode": {"da": 3, "de": 3}}


def test_counts_what_needed_a_person():
    n = report()["needed_a_person"]
    assert (n["requests"], n["escalations"], n["fixes_waiting_for_approval"]) == (2, 1, 1)
    assert n["share_handled_without_a_person"] == round(1 - 2 / 6, 3)


def test_approval_outcomes_and_decision_time():
    a = report()["approvals"]
    assert a["requested"] == 3
    assert a["by_outcome"] == {"executed": 1, "rejected": 1, "expired": 1}
    assert a["median_decision_minutes"] == 10.0  # 5 and 15 minutes
    assert a["approvers"] == {"Jane Doe": 1, "Sam Lee": 1}


def test_sources_problems_and_safety_events():
    r = report()
    assert r["analyst_answers_by_source"] == {"ask_genie": 2, "compute_metric": 1}
    assert r["problems_by_catalog_id"] == {"B2": 1, "B1": 1}
    assert r["safety_events"] == {"identifier requests refused": 1, "answers with small groups suppressed": 1}


def test_markdown_renders():
    text = human_work.to_markdown(report())
    assert "Needed a person: **2**" in text
    assert "Handled without a person: **67%**" in text


class _ScriptedClaude:
    def __init__(self, steps):
        self.steps = steps

    def run_tool_loop(self, system, messages, tools, dispatch, max_turns=6):
        for name, tool_input in self.steps:
            dispatch(name, tool_input)
        return "done"


class _Bridge:
    def dispatch(self, tool_name, tool_input):
        return ToolResult(
            tool_use_id="", content=json.dumps({"ok": True, "paged": tool_name == "notify_and_page"})
        )


def test_end_to_end_on_a_real_audit_log(tmp_path, capsys):
    audit = tmp_path / "audit.jsonl"
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"incidents": []}))
    for steps in ([("check_job_status", {})], [("notify_and_page", {"message": "hard stop"})]):
        OrchestratorAgent(
            claude=_ScriptedClaude(steps), state_path=state, audit_log_path=audit, mcp_bridge=_Bridge()
        ).handle("health check", mode="de")

    assert human_work.main(["--audit-log-path", str(audit), "--state-path", str(state), "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["requests"] == {"total": 2, "by_mode": {"de": 2}}
    assert printed["needed_a_person"]["escalations"] == 1
