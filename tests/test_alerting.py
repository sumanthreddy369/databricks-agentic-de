"""Alerts reach a person once per problem (agent/tools/alerting.py), and the
scheduled health check fails its run only for something new - so a known,
unresolved problem doesn't alert or email every 15 minutes."""

import json
from datetime import UTC, datetime, timedelta

import httpx

from agent import healthcheck
from agent.llm import ToolResult
from agent.orchestrator import OrchestratorAgent
from agent.rules_planner import RulesPlanner
from agent.tools import alerting, pipeline_health

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


def test_without_a_webhook_the_incident_is_recorded_once(monkeypatch):
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    state = {}

    first = alerting.send_alert(state, "B2 contract break", now=NOW)
    again = alerting.send_alert(state, "B2 contract break", now=NOW + timedelta(hours=1))
    later = alerting.send_alert(state, "B2 contract break", now=NOW + timedelta(hours=25))

    assert first == {"fingerprint": first["fingerprint"], "new": True, "sent": False, "channel": "none configured"}
    assert again["new"] is False and "already alerted" in again["reason"]
    assert later["new"] is True  # re-alerts after ALERT_REPEAT_HOURS (24 by default)


def test_webhook_gets_a_slack_style_post_and_failures_retry_next_time(monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.example.com/T000/B000/xyz")
    posts, status = [], {"code": 500}

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(json.loads(request.content))
        return httpx.Response(status["code"])

    transport = httpx.MockTransport(handler)
    state = {}

    failed = alerting.send_alert(state, "needs a person", now=NOW, transport=transport)
    status["code"] = 200
    sent = alerting.send_alert(state, "needs a person", now=NOW + timedelta(minutes=15), transport=transport)

    assert failed["sent"] is False and failed["error"] == "HTTPStatusError"
    assert sent == {"fingerprint": sent["fingerprint"], "new": True, "sent": True, "channel": "webhook"}
    assert posts == [{"text": "needs a person"}, {"text": "needs a person"}]
    assert "hooks.example.com" not in json.dumps(sent)  # the URL is a secret: never returned


def test_notify_and_page_records_when_and_how_a_person_was_told(tmp_path, monkeypatch):
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    state_path = tmp_path / "state.json"
    state_path.write_text("{}")

    first = json.loads(pipeline_health.notify_and_page(state_path, "msg 1", fingerprint="identity").content)
    second = json.loads(pipeline_health.notify_and_page(state_path, "msg 2", fingerprint="identity").content)

    assert first["paged"] is True and first["new_incident"] is True
    assert second["new_incident"] is False
    incidents = json.loads(state_path.read_text())["incidents"]
    assert [i["alert"]["new"] for i in incidents] == [True, False]
    assert all("at" in i for i in incidents)


class _InProcessBridge:
    """The real pipeline tools, without the MCP subprocess."""

    def __init__(self, failing):
        self.failing = failing

    def dispatch(self, tool, tool_input):
        if tool == "check_expectation_metrics":
            return ToolResult(
                tool_use_id="", content=json.dumps({"ok": False, "failing_expectations": self.failing})
            )
        if tool == "check_job_status":
            return ToolResult(tool_use_id="", content=json.dumps({"ok": True, "unhealthy": {}}))
        if tool == "lookup_problem":
            from agent.tools.problem_catalog import lookup_problem

            return lookup_problem(query=tool_input["query"])
        if tool == "notify_and_page":
            return pipeline_health.notify_and_page(
                tool_input["state_path"], tool_input["message"], tool_input.get("fingerprint")
            )
        return ToolResult(tool_use_id="", content=json.dumps({"ok": True}))


def _check(tmp_path, failing, capsys):
    agent = OrchestratorAgent(
        claude=RulesPlanner(),
        state_path=tmp_path / "state.json",
        audit_log_path=tmp_path / "audit.jsonl",
        mcp_bridge=_InProcessBridge(failing),
    )
    code = healthcheck.run(["--state-path", str(tmp_path / "state.json")], agent=agent)
    capsys.readouterr()
    return code


def test_a_known_problem_fails_the_first_run_only_even_as_counts_change(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    identity = "cat.silver.silver_patient_identity_conflicts"

    def failing(n):
        return [
            {"pipeline": "p", "table": identity, "expectation": "one_identity_per_patient_id", "failure_count": n}
        ]

    assert _check(tmp_path, failing(16), capsys) == healthcheck.EXIT_ESCALATED
    assert _check(tmp_path, failing(17), capsys) == healthcheck.EXIT_OK  # same problem, new count: no new alert
    # A different problem is new again.
    other = failing(16) + [
        {
            "pipeline": "p",
            "table": "cat.silver.silver_contract_checked_events",
            "expectation": "known_event_type",
            "failure_count": 1,
        }
    ]
    assert _check(tmp_path, other, capsys) == healthcheck.EXIT_ESCALATED
