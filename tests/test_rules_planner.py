"""The deterministic health-check planner (agent/rules_planner.py) and the
environment the MCP tool server gets (agent/mcp_bridge.py) - both found on
the first live run against Databricks Free Edition."""

import json

from agent.llm import ToolResult
from agent.mcp_bridge import server_environment
from agent.rules_planner import RulesPlanner


def test_tool_server_gets_the_databricks_settings_but_not_the_model_key(monkeypatch):
    """Without forwarding, the MCP server never saw DATABRICKS_* and the live
    health check silently read the local state file - and said "healthy"."""
    monkeypatch.setenv("DATABRICKS_HOST", "https://example.cloud.databricks.com")
    monkeypatch.setenv("DATABRICKS_PIPELINE_IDS", '{"p": "1"}')
    monkeypatch.setenv("PIPELINE_STATE_PATH", "/Volumes/x/state.json")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-needed-by-tools")

    env = server_environment()

    assert env["DATABRICKS_HOST"] == "https://example.cloud.databricks.com"
    assert env["DATABRICKS_PIPELINE_IDS"] == '{"p": "1"}'
    assert env["PIPELINE_STATE_PATH"] == "/Volumes/x/state.json"
    assert "ANTHROPIC_API_KEY" not in env
    assert "PATH" in env


CATALOG = {
    "plausible_vital_value failing in silver_fct_vitals": {
        "id": "B1",
        "title": "Drop spike",
        "autonomy_level": "L2",
    },
    "one_identity_per_patient_id failing in silver_patient_identity_conflicts": {
        "id": "M2",
        "title": "Identity",
        "autonomy_level": "L1",
    },
    "freshness_lag failing in silver_fct_encounters": {"id": "X0", "title": "Watch", "autonomy_level": "L0"},
}


class _Dispatch:
    def __init__(self, failing, unhealthy=None):
        self.failing, self.unhealthy, self.calls = failing, unhealthy or {}, []

    def __call__(self, tool, tool_input):
        self.calls.append((tool, tool_input))
        if tool == "check_expectation_metrics":
            payload = {"ok": not self.failing, "failing_expectations": self.failing}
        elif tool == "check_job_status":
            payload = {"ok": not self.unhealthy, "unhealthy": self.unhealthy}
        elif tool == "lookup_problem":
            match = CATALOG.get(tool_input["query"])
            payload = {"matches": [match] if match else []}
        else:
            payload = {"ok": True}
        return ToolResult(tool_use_id="", content=json.dumps(payload))


def _failing(table, expectation, n):
    return {"pipeline": "p", "table": f"cat.silver.{table}", "expectation": expectation, "failure_count": n}


def test_healthy_pipeline_takes_no_action():
    dispatch = _Dispatch([])
    answer = RulesPlanner().run_tool_loop("", [], [], dispatch)
    assert answer.startswith("Healthy")
    assert [c[0] for c in dispatch.calls] == ["check_expectation_metrics", "check_job_status"]


def test_actions_follow_the_catalog_level_and_never_exceed_it():
    dispatch = _Dispatch(
        [
            _failing("silver_fct_vitals", "plausible_vital_value", 286),
            _failing("silver_patient_identity_conflicts", "one_identity_per_patient_id", 16),
            _failing("silver_fct_encounters", "freshness_lag", 3),
            _failing("silver_new_table", "brand_new_check", 1),
        ]
    )
    RulesPlanner().run_tool_loop("", [], [], dispatch)

    actions = [
        c for c in dispatch.calls if c[0] in ("quarantine_bad_records", "notify_and_page", "restart_pipeline")
    ]
    # L2 -> a fix is requested (the orchestrator's approval gate decides whether it runs)
    assert (
        "quarantine_bad_records",
        {"table": "silver_fct_vitals", "expectation": "plausible_vital_value"},
    ) in actions
    # L1 and unknown problems -> one page; L0 -> nothing
    pages = [c for c in actions if c[0] == "notify_and_page"]
    assert len(pages) == 1
    assert "one_identity_per_patient_id" in pages[0][1]["message"]
    assert "brand_new_check" in pages[0][1]["message"] and "new problem" in pages[0][1]["message"]
    assert "freshness_lag" not in pages[0][1]["message"]
    assert not any(c[0] == "restart_pipeline" for c in actions)


def test_unreadable_pipeline_state_is_escalated_not_called_healthy():
    def dispatch(tool, tool_input):
        if tool in ("check_expectation_metrics", "check_job_status"):
            return ToolResult(tool_use_id="", content=json.dumps({"ok": False, "error": "403 forbidden"}))
        if tool == "lookup_problem":
            return ToolResult(tool_use_id="", content=json.dumps({"matches": []}))
        return ToolResult(tool_use_id="", content=json.dumps({"ok": True}))

    answer = RulesPlanner().run_tool_loop("", [], [], dispatch)
    assert "Escalated" in answer and "could not read pipeline state" in answer


def test_inside_a_databricks_job_the_run_identity_is_used_without_a_token(monkeypatch):
    from agent import databricks_client

    monkeypatch.delenv("DATABRICKS_HOST", raising=False)
    monkeypatch.delenv("DATABRICKS_TOKEN", raising=False)
    monkeypatch.setenv("DATABRICKS_PIPELINE_IDS", '{"streaming_patient_pipeline": "abc"}')
    monkeypatch.setattr(
        databricks_client, "_runtime_credentials", lambda: ("https://ws.example.com", "runtime-token")
    )

    config = databricks_client.load_config()

    assert (config.host, config.token) == ("https://ws.example.com", "runtime-token")
    assert config.pipeline_ids == {"streaming_patient_pipeline": "abc"}


def test_outside_databricks_there_are_no_runtime_credentials(monkeypatch):
    from agent import databricks_client

    monkeypatch.delenv("DATABRICKS_RUNTIME_VERSION", raising=False)
    assert databricks_client._runtime_credentials() is None
