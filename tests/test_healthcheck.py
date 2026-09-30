"""Proves agent/healthcheck.py (the scheduled job's entry point) maps agent
outcomes to the exit codes the Databricks job status depends on, and
initializes a missing state file. Uses a scripted fake Claude and a fake MCP
bridge — zero network calls. Says nothing about the job running on a real
workspace, which it hasn't.
"""

import json
import tomllib
from pathlib import Path

import yaml

from agent import healthcheck
from agent.llm import ToolResult
from agent.orchestrator import OrchestratorAgent

REPO_ROOT = Path(__file__).resolve().parent.parent


class _ScriptedClaude:
    """Calls the given tools through dispatch, then answers."""

    def __init__(self, tool_calls=(), answer="All pipelines healthy.", raises=False):
        self.tool_calls, self.answer, self.raises = tool_calls, answer, raises

    def run_tool_loop(self, system, messages, tools, dispatch, max_turns=6):
        if self.raises:
            raise RuntimeError("Claude API unreachable")
        for name, tool_input in self.tool_calls:
            dispatch(name, tool_input)
        return self.answer


class _FakeBridge:
    def dispatch(self, tool_name, tool_input):
        return ToolResult(tool_use_id="", content=json.dumps({"ok": True, "tool": tool_name}))


def _agent(tmp_path, claude):
    return OrchestratorAgent(
        claude=claude,
        state_path=tmp_path / "state.json",
        audit_log_path=tmp_path / "audit.jsonl",
        mcp_bridge=_FakeBridge(),
    )


def _run(tmp_path, claude, capsys):
    code = healthcheck.run(
        ["--state-path", str(tmp_path / "state.json"), "--audit-log-path", str(tmp_path / "audit.jsonl")],
        agent=_agent(tmp_path, claude),
    )
    # structlog also writes to stdout; the JSON summary is always the last line.
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_healthy_run_exits_zero_and_prints_result(tmp_path, capsys):
    code, printed = _run(tmp_path, _ScriptedClaude(tool_calls=[("check_job_status", {})]), capsys)

    assert code == healthcheck.EXIT_OK
    assert printed["mode"] == "de"
    assert printed["tool_calls"] == ["check_job_status"]


def test_escalation_exits_non_zero(tmp_path, capsys):
    claude = _ScriptedClaude(tool_calls=[("notify_and_page", {"message": "known_event_type failed"})])

    code, printed = _run(tmp_path, claude, capsys)

    assert code == healthcheck.EXIT_ESCALATED
    assert "notify_and_page" in printed["tool_calls"]


def test_agent_unavailable_exits_non_zero(tmp_path, capsys):
    code, _ = _run(tmp_path, _ScriptedClaude(raises=True), capsys)

    assert code == healthcheck.EXIT_AGENT_UNAVAILABLE


def test_missing_state_file_is_initialized_with_remediation_enabled(tmp_path, capsys):
    _run(tmp_path, _ScriptedClaude(), capsys)

    state = json.loads((tmp_path / "state.json").read_text())
    assert state["autonomous_remediation_enabled"] is True


def test_existing_state_file_is_not_overwritten(tmp_path, capsys):
    (tmp_path / "state.json").write_text(json.dumps({"autonomous_remediation_enabled": False}))

    _run(tmp_path, _ScriptedClaude(), capsys)

    assert json.loads((tmp_path / "state.json").read_text()) == {"autonomous_remediation_enabled": False}


def test_relative_state_path_is_made_absolute_before_crossing_the_mcp_boundary(tmp_path, monkeypatch):
    """The MCP server subprocess runs with the package location as its cwd,
    so a relative state_path must never be passed to it as-is."""
    seen = []

    class _RecordingBridge:
        def dispatch(self, tool_name, tool_input):
            seen.append(tool_input["state_path"])
            return ToolResult(tool_use_id="", content=json.dumps({"ok": True}))

    monkeypatch.chdir(tmp_path)
    orchestrator = OrchestratorAgent(
        claude=_ScriptedClaude(),
        state_path="state.json",
        audit_log_path=tmp_path / "audit.jsonl",
        mcp_bridge=_RecordingBridge(),
    )
    orchestrator._dispatch("check_job_status", {})

    assert seen == [str((tmp_path / "state.json").resolve())]


def test_entry_point_named_by_the_job_is_declared_in_the_wheel():
    """resources/workflows.yml runs `entry_point: orchestrator_healthcheck`;
    that name must exist as a console script pointing at healthcheck.main."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    job = yaml.safe_load((REPO_ROOT / "resources" / "workflows.yml").read_text())["resources"]["jobs"][
        "agent_pipeline_healthcheck"
    ]
    [task] = job["tasks"]

    entry_point = task["python_wheel_task"]["entry_point"]
    assert pyproject["project"]["scripts"][entry_point] == "agent.healthcheck:main"
    assert task["python_wheel_task"]["package_name"] == pyproject["project"]["name"].replace("-", "_")
    assert task["libraries"] == [{"whl": "../dist/*.whl"}]
    assert "mcp_server" in pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    env = job["job_clusters"][0]["new_cluster"]["spark_env_vars"]
    assert env["PIPELINE_STATE_PATH"].startswith("/Volumes/")
    assert env["AUDIT_LOG_PATH"].startswith("/Volumes/")
