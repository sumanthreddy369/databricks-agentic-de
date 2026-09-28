"""Real MCP (Model Context Protocol) server exposing this project's existing
tool functions — `agent/tools/pipeline_health.py`, `agent/tools/data_query.py`,
and `agent/tools/anomaly_score.py` — over the standard MCP stdio transport.

This does not reimplement any tool logic: every function below is a thin
adapter that calls straight into the same Python functions
`agent/orchestrator.py` used to call in-process, and wraps their existing
`agent.llm.ToolResult` return value into an MCP `CallToolResult`. See
`agent/mcp_bridge.py` for the client side (an `MCPToolBridge` that launches
this module as a subprocess, discovers these tools via the MCP protocol, and
dispatches calls to it) and `docs/architecture.md`'s "MCP tool architecture"
section for why this project adopted MCP instead of only ever calling these
functions in-process.

Run directly for manual/manual-protocol testing:

    uv run python -m mcp_server.server

`tests/test_mcp_server_tools.py` spins this up as a real subprocess and talks
to it over stdio with a real MCP `ClientSession` — no network calls, since
stdio is a local pipe, not a socket.
"""

from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent

from agent.llm import ToolResult
from agent.tools import anomaly_score as anomaly_score_module
from agent.tools import data_query, pipeline_health

mcp_app = MCPServer(
    name="databricks-agentic-de-tools",
    instructions=(
        "Pipeline-health, governed-data-query, and vitals-anomaly-scoring tools for the "
        "databricks-agentic-de healthcare streaming pipeline's orchestrator agent."
    ),
)


def _to_call_tool_result(result: ToolResult) -> CallToolResult:
    """Converts this project's existing `ToolResult(content, is_error)` shape
    into an MCP `CallToolResult` with the SAME content string and error flag
    — `func_metadata.convert_result` (mcp's own tool-return-value converter)
    passes a `CallToolResult` a tool function returns straight through
    unchanged, so this is an exact, lossless round trip in both directions;
    see `agent/mcp_bridge.py:MCPToolBridge.dispatch` for the reverse
    conversion back into a `ToolResult` on the client side.
    """
    return CallToolResult(content=[TextContent(type="text", text=result.content)], is_error=result.is_error)


@mcp_app.tool()
def check_expectation_metrics(state_path: str):
    """Check DLT expectation failure counts across all tables in the given
    pipeline-state JSON file."""
    return _to_call_tool_result(pipeline_health.check_expectation_metrics(state_path))


@mcp_app.tool()
def check_job_status(state_path: str):
    """Check the status of pipeline jobs recorded in the given pipeline-state
    JSON file."""
    return _to_call_tool_result(pipeline_health.check_job_status(state_path))


@mcp_app.tool()
def detect_schema_drift(state_path: str):
    """Diff the observed patient_events schema (recorded in the given
    pipeline-state JSON file) against the common.contracts envelope
    contract."""
    return _to_call_tool_result(pipeline_health.detect_schema_drift(state_path))


@mcp_app.tool()
def quarantine_bad_records(state_path: str, table: str, expectation: str):
    """Clear a failing expectation's failure count on `table` by quarantining
    its bad records, recorded in the given pipeline-state JSON file."""
    return _to_call_tool_result(pipeline_health.quarantine_bad_records(state_path, table, expectation))


@mcp_app.tool()
def restart_pipeline(state_path: str, pipeline_name: str):
    """Restart a named pipeline/job, recorded in the given pipeline-state
    JSON file."""
    return _to_call_tool_result(pipeline_health.restart_pipeline(state_path, pipeline_name))


@mcp_app.tool()
def notify_and_page(state_path: str, message: str):
    """Escalate to a human on-call engineer; appends an incident to the given
    pipeline-state JSON file."""
    return _to_call_tool_result(pipeline_health.notify_and_page(state_path, message))


@mcp_app.tool()
def query_gold_table(table: str, filters: dict | None = None, db_path: str | None = None):
    """Query a governed Gold table by name, with an optional {column: value}
    equality filter map. Returns RAW, UNMASKED rows — see this module's
    docstring and agent/orchestrator.py's own comment on why DA-mode keeps
    calling `agent.tools.data_query.query_gold_table` directly in-process
    instead of through this MCP tool: PHI masking must be the very next thing
    that happens to a raw row, and this tool exists for completeness/testing
    of the MCP surface, not as the production DA-mode call path."""
    resolved_db_path = Path(db_path) if db_path else None
    return _to_call_tool_result(data_query.query_gold_table(table, filters, db_path=resolved_db_path))


@mcp_app.tool(name="score_vitals_anomaly")
def _score_vitals_anomaly_tool(vitals: dict):
    """Score one multi-vital reading (heart_rate, spo2, resp_rate, temp_c,
    sbp, dbp) for JOINT anomaly risk via the local ONNX IsolationForest
    model trained by ml/train_anomaly_model.py."""
    return _to_call_tool_result(anomaly_score_module.score_vitals_anomaly(vitals))


if __name__ == "__main__":
    mcp_app.run()
