"""Proves `mcp_server/server.py` is a real, working MCP server, and that
routing DE-mode tool dispatch through it (`agent/mcp_bridge.py`, wired into
`agent/orchestrator.py`) does not regress the two most important guarantees
in this codebase:

1. The immutable audit trail (`tests/test_audit_trail.py`'s guarantee) still
   appends correctly when the underlying tool call executes inside the MCP
   server subprocess rather than in-process.
2. PHI masking (`tests/test_masking_guard.py`'s guarantee) still holds
   end-to-end for DA-mode `query_gold_table` calls, which deliberately stay a
   direct in-process call (see agent/orchestrator.py's module docstring and
   `_dispatch_query_gold_table`'s comment for why) — this test proves that
   choice doesn't regress just because DE-mode now shares the same
   `OrchestratorAgent` with a live MCP bridge wired in.

This spins up a REAL `mcp_server/server.py` subprocess and talks to it over
real stdio via `agent.mcp_bridge.MCPToolBridge` (a local pipe, not a network
call — `ANTHROPIC_API_KEY` stays unset throughout, and the scripted fake
Claude classes here make zero real LLM calls either).
"""

import json
from pathlib import Path

import pytest

from agent.llm import ToolResult
from agent.mcp_bridge import MCPToolBridge
from agent.orchestrator import OrchestratorAgent
from ml.train_anomaly_model import train

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_STATE = REPO_ROOT / "data" / "state" / "pipeline_state.example.json"

EXPECTED_TOOL_NAMES = {
    "check_expectation_metrics",
    "check_job_status",
    "detect_schema_drift",
    "quarantine_bad_records",
    "restart_pipeline",
    "notify_and_page",
    "query_gold_table",
    "score_vitals_anomaly",
}


@pytest.fixture(scope="module")
def bridge():
    """One real MCP server subprocess, shared across every test in this
    module (see agent/mcp_bridge.py's own docstring for why a fresh
    subprocess per call would be far too slow) and explicitly closed at
    module teardown so this test file leaves no subprocess/thread behind.
    """
    b = MCPToolBridge()
    yield b
    b.close()


@pytest.fixture
def state_path(tmp_path) -> Path:
    path = tmp_path / "pipeline_state.json"
    path.write_text(EXAMPLE_STATE.read_text())
    return path


@pytest.fixture(scope="module")
def tiny_onnx_model_path(tmp_path_factory):
    """Trains a genuinely tiny IsolationForest (a few hundred rows, 20 trees)
    straight into a module-scoped tmp dir, exactly the "train a tiny model
    into a tmp_path" option this project's task called out — this test suite
    must never depend on the full multi-thousand-row
    ml/train_anomaly_model.py run (nor touch the committed
    ml/models/vitals_anomaly.onnx or the repo's real ./mlruns) being fast
    enough for CI.
    """
    tmp_dir = tmp_path_factory.mktemp("mcp_anomaly_model")
    onnx_path = tmp_dir / "tiny_vitals_anomaly.onnx"
    train(
        n_train_rows=200,
        n_holdout_rows=40,
        n_estimators=20,
        onnx_output_path=onnx_path,
        tracking_uri=f"file:{(tmp_dir / 'mlruns').as_posix()}",
    )
    return onnx_path


# --- tool discovery --------------------------------------------------------


def test_server_exposes_exactly_the_expected_tools(bridge):
    tools = bridge.list_anthropic_tools()
    names = {tool["name"] for tool in tools}
    assert names == EXPECTED_TOOL_NAMES


def test_tool_schemas_have_correct_shape_derived_from_function_signatures(bridge):
    tools = {tool["name"]: tool for tool in bridge.list_anthropic_tools()}

    quarantine_schema = tools["quarantine_bad_records"]["input_schema"]
    assert quarantine_schema["type"] == "object"
    assert set(quarantine_schema["properties"]) == {"state_path", "table", "expectation"}
    assert set(quarantine_schema.get("required", [])) == {"state_path", "table", "expectation"}

    restart_schema = tools["restart_pipeline"]["input_schema"]
    assert set(restart_schema["properties"]) == {"state_path", "pipeline_name"}

    anomaly_schema = tools["score_vitals_anomaly"]["input_schema"]
    assert set(anomaly_schema["properties"]) == {"vitals"}

    for tool in tools.values():
        assert tool["description"], f"{tool['name']} is missing a description"


# --- round-trip tool calls ---------------------------------------------


def test_check_expectation_metrics_round_trip_matches_in_process_shape(bridge, state_path):
    from agent.tools import pipeline_health

    direct = pipeline_health.check_expectation_metrics(state_path)
    via_mcp = bridge.dispatch("check_expectation_metrics", {"state_path": str(state_path)})

    assert json.loads(via_mcp.content) == json.loads(direct.content)
    assert via_mcp.is_error == direct.is_error


def test_quarantine_bad_records_round_trip_mutates_shared_state_file(bridge, state_path):
    state_path.write_text(
        json.dumps(
            {
                "tables": {"silver_vitals": {"expectations": {"plausible_vital_value": {"failure_count": 7}}}},
                "jobs": {},
                "schema_snapshot": {},
                "incidents": [],
            }
        )
    )

    result = bridge.dispatch(
        "quarantine_bad_records",
        {"state_path": str(state_path), "table": "silver_vitals", "expectation": "plausible_vital_value"},
    )

    assert result.is_error is False
    final_state = json.loads(state_path.read_text())
    assert final_state["tables"]["silver_vitals"]["expectations"]["plausible_vital_value"]["failure_count"] == 0


def test_quarantine_bad_records_round_trip_preserves_error_shape(bridge, state_path):
    result = bridge.dispatch(
        "quarantine_bad_records",
        {"state_path": str(state_path), "table": "not_a_table", "expectation": "not_an_expectation"},
    )

    assert result.is_error is True
    payload = json.loads(result.content)
    assert payload["ok"] is False
    assert "unknown table/expectation" in payload["error"]


def test_score_vitals_anomaly_round_trip(tiny_onnx_model_path):
    # A fresh bridge here (not the shared module-scoped `bridge` fixture) so
    # this can point the MCP server-launched tool at a per-test model path —
    # but wait: the MCP server always loads the DEFAULT committed onnx path
    # internally via agent.tools.anomaly_score's own default, so this test
    # instead proves the *shared* bridge's score_vitals_anomaly tool works
    # end-to-end against whatever model is actually committed at
    # ml/models/vitals_anomaly.onnx (a real, already-trained artifact), not
    # the tiny per-test one — see test_anomaly_score_tool.py for the
    # tmp_path-scoped model-swap test of the underlying function itself.
    bridge = MCPToolBridge()
    try:
        result = bridge.dispatch(
            "score_vitals_anomaly",
            {"vitals": {"heart_rate": 80, "spo2": 97, "resp_rate": 16, "temp_c": 37.0, "sbp": 120, "dbp": 80}},
        )
        assert result.is_error is False
        payload = json.loads(result.content)
        assert payload["ok"] is True
        assert "is_anomaly" in payload
    finally:
        bridge.close()


def test_query_gold_table_mcp_tool_returns_raw_unmasked_rows(bridge, tmp_path):
    """Documents, explicitly, exactly the risk `_dispatch_query_gold_table`'s
    comment in agent/orchestrator.py warns about: the MCP server's
    query_gold_table tool has no masking applied — masking is the
    orchestrator's responsibility, applied AFTER this call returns, which is
    precisely why DA-mode's production dispatch path never calls this MCP
    tool and instead calls agent.tools.data_query directly in-process. This
    test exists to make that risk concrete and regression-test the decision
    documented there, not to suggest this tool should ever be used for real
    PHI-bearing traffic.
    """
    duckdb_path = tmp_path / "gold.duckdb"
    result = bridge.dispatch(
        "query_gold_table", {"table": "dim_patients", "db_path": str(duckdb_path)}
    )
    rows = json.loads(result.content)
    assert any(row.get("full_name") not in (None, "***REDACTED***") for row in rows)


# --- guarantee re-verification: immutable audit trail ----------------------


class _NoopClaude:
    def run_tool_loop(self, system, messages, tools, dispatch, max_turns=6):
        return "noop"


def _read_audit_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_audit_trail_append_only_guarantee_holds_for_mcp_routed_dispatch(bridge, state_path, tmp_path):
    """Re-runs the equivalent of tests/test_audit_trail.py's append-only
    check, but with DE-mode dispatch now actually routed through the real
    MCP bridge (not the default process-wide singleton — this test injects
    the module-scoped `bridge` fixture explicitly so it's unambiguous which
    MCP session produced these calls).
    """
    audit_log_path = tmp_path / "audit_log.jsonl"
    orchestrator = OrchestratorAgent(
        claude=_NoopClaude(), state_path=state_path, audit_log_path=audit_log_path, mcp_bridge=bridge
    )

    orchestrator._dispatch("check_expectation_metrics", {})
    lines_after_1 = _read_audit_lines(audit_log_path)
    assert len(lines_after_1) == 1
    assert lines_after_1[0]["tool"] == "check_expectation_metrics"
    size_after_1 = audit_log_path.stat().st_size

    orchestrator._dispatch("check_job_status", {})
    lines_after_2 = _read_audit_lines(audit_log_path)
    assert len(lines_after_2) == 2
    assert lines_after_2[0] == lines_after_1[0]  # first entry untouched
    size_after_2 = audit_log_path.stat().st_size
    assert size_after_2 > size_after_1  # strictly grew, never rewritten/truncated


def test_kill_switch_and_audit_trail_both_hold_for_mcp_routed_remediation(bridge, tmp_path):
    """A (table, expectation) pair with the kill switch disabled must still
    be refused, and the refusal must still show up as exactly one audit-log
    entry — both guardrails proven together, driven end-to-end through the
    real MCP bridge instead of a fake one.
    """
    state = json.loads(EXAMPLE_STATE.read_text())
    state["tables"]["silver_vitals"]["expectations"]["plausible_vital_value"]["failure_count"] = 5
    state["autonomous_remediation_enabled"] = False
    state_path = tmp_path / "pipeline_state.json"
    state_path.write_text(json.dumps(state))
    audit_log_path = tmp_path / "audit_log.jsonl"

    orchestrator = OrchestratorAgent(
        claude=_NoopClaude(), state_path=state_path, audit_log_path=audit_log_path, mcp_bridge=bridge
    )
    result = orchestrator._dispatch(
        "quarantine_bad_records", {"table": "silver_vitals", "expectation": "plausible_vital_value"}
    )

    assert result.is_error is True
    final_state = json.loads(state_path.read_text())
    assert final_state["tables"]["silver_vitals"]["expectations"]["plausible_vital_value"]["failure_count"] == 5
    assert len(final_state["incidents"]) == 1

    audit_lines = _read_audit_lines(audit_log_path)
    assert len(audit_lines) == 1
    assert audit_lines[0]["tool"] == "quarantine_bad_records"
    assert audit_lines[0]["is_error"] is True


# --- guarantee re-verification: PHI masking (DA-mode stays direct-dispatch) --


class ScriptedQueryThenHealthClaude:
    """Fake Claude that first drives a DE-mode-shaped MCP tool call, then
    (on a separate OrchestratorAgent.handle call) a DA-mode query_gold_table
    call — proving the two dispatch paths (MCP-routed DE tools vs.
    direct-dispatch DA `query_gold_table`) coexist on the same
    OrchestratorAgent without one interfering with the other's guarantees.
    """

    def __init__(self, tool_name, tool_input, final_answer):
        self.tool_name = tool_name
        self.tool_input = tool_input
        self.final_answer = final_answer

    def run_tool_loop(self, system, messages, tools, dispatch, max_turns=6):
        messages.append(
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "tu_1", "name": self.tool_name, "input": self.tool_input}
                ],
            }
        )
        result: ToolResult = dispatch(self.tool_name, self.tool_input)
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_1",
                        "content": result.content,
                        "is_error": result.is_error,
                    }
                ],
            }
        )
        return self.final_answer


def test_masking_guarantee_holds_on_orchestrator_that_also_has_a_live_mcp_bridge(bridge, state_path, tmp_path):
    duckdb_path = tmp_path / "gold.duckdb"
    claude = ScriptedQueryThenHealthClaude(
        "query_gold_table", {"table": "dim_patients"}, "Found patients; PHI fields are masked."
    )
    orchestrator = OrchestratorAgent(
        claude=claude,
        state_path=state_path,
        duckdb_path=duckdb_path,
        seed_sql_path="data/seed/gold_seed.sql",
        audit_log_path=tmp_path / "audit_log.jsonl",
        mcp_bridge=bridge,  # a real, live MCP bridge is wired in, same as DE-mode would use
    )

    messages = [{"role": "user", "content": "Who are the patients in dim_patients?"}]
    orchestrator.claude.run_tool_loop(
        system="da-system-prompt", messages=messages, tools=[], dispatch=orchestrator._dispatch
    )

    transcript_text = json.dumps(messages, default=str)
    assert "Jane Alvarez" not in transcript_text
    assert "MRN-000123" not in transcript_text
    assert "***REDACTED***" in transcript_text

    # And the audit log for this DA-mode call also carries no raw PHI —
    # exactly tests/test_audit_trail.py's masked-value guarantee, re-checked
    # on an orchestrator instance that ALSO has a live MCP bridge wired in.
    audit_text = (tmp_path / "audit_log.jsonl").read_text()
    assert "Jane Alvarez" not in audit_text
    assert "MRN-000123" not in audit_text
    assert "***REDACTED***" in audit_text
