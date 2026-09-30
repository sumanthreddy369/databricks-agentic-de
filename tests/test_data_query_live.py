"""Proves agent/tools/data_query.py's live-workspace path and its shared
filter validation, with a mocked Databricks REST transport — zero network
calls, no real warehouse.

The most important test here is the last one: rows that come back from the
live path, with a raw `full_name`/`mrn` in them (as if Unity Catalog's
column mask had been misconfigured), are still masked by the orchestrator
before they reach a ToolResult. The masking guarantee doesn't depend on
which backend served the rows.
"""

import json

import httpx
import pytest

from agent.databricks_client import DatabricksClient
from agent.orchestrator import OrchestratorAgent
from agent.tools import data_query
from agent.tools.data_query import query_gold_table
from common.contracts import MAX_TOOL_RESULT_ROWS

SEED_SQL = "data/seed/gold_seed.sql"

RAW_LIVE_PATIENT = ["pt_00001", "MRN-000123", "Jane Alvarez", "1968-04-02", "female", "midwest"]
PATIENT_COLUMNS = [
    {"name": name, "type_name": "STRING"}
    for name in ("patient_id", "mrn", "full_name", "birth_date", "gender", "region")
]


@pytest.fixture
def live_workspace(monkeypatch):
    """Configures the live DA path and records every request sent to it."""
    monkeypatch.setenv("DATABRICKS_HOST", "https://example.gcp.databricks.com")
    monkeypatch.setenv("DATABRICKS_TOKEN", "test-token")
    monkeypatch.setenv("DATABRICKS_WAREHOUSE_ID", "wh-123")
    monkeypatch.delenv("GCP_PROJECT_ID", raising=False)

    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "status": {"state": "SUCCEEDED"},
                "manifest": {"schema": {"columns": PATIENT_COLUMNS}},
                "result": {"data_array": [RAW_LIVE_PATIENT]},
            },
        )

    monkeypatch.setattr(
        data_query,
        "DatabricksClient",
        lambda config: DatabricksClient(config, transport=httpx.MockTransport(handler)),
    )
    return requests


def test_live_query_targets_gold_schema_with_bound_parameters(live_workspace):
    result = query_gold_table("dim_patients", {"patient_id": "pt_00001", "region": "midwest"})

    assert result.is_error is False
    assert json.loads(result.content)[0]["patient_id"] == "pt_00001"
    [request] = live_workspace
    assert request["statement"] == (
        "SELECT * FROM healthcare_agentic_de.gold.dim_patients "
        f"WHERE patient_id = :p0 AND region = :p1 LIMIT {MAX_TOOL_RESULT_ROWS + 1}"
    )
    assert request["parameters"] == [
        {"name": "p0", "value": "pt_00001", "type": "STRING"},
        {"name": "p1", "value": "midwest", "type": "STRING"},
    ]


def test_live_parameters_carry_their_sql_types(live_workspace):
    query_gold_table("fct_vitals", {"value": 88.0, "reading_count": 3, "flagged": True})

    assert live_workspace[0]["parameters"] == [
        {"name": "p0", "value": "88.0", "type": "DOUBLE"},
        {"name": "p1", "value": "3", "type": "BIGINT"},
        {"name": "p2", "value": "true", "type": "BOOLEAN"},
    ]


def test_explicit_local_db_path_never_goes_live(live_workspace, tmp_path):
    result = query_gold_table("dim_patients", db_path=tmp_path / "gold.duckdb", seed_sql_path=SEED_SQL)

    assert result.is_error is False
    assert live_workspace == []


@pytest.mark.parametrize("column", ["full_name", "mrn"])
def test_filtering_on_a_masked_column_is_refused_on_both_backends(live_workspace, tmp_path, column):
    live = query_gold_table("dim_patients", {column: "Jane Alvarez"})
    local = query_gold_table(
        "dim_patients", {column: "Jane Alvarez"}, db_path=tmp_path / "gold.duckdb", seed_sql_path=SEED_SQL
    )

    for result in (live, local):
        assert result.is_error is True
        assert "masked column" in json.loads(result.content)["error"]
    assert live_workspace == []


@pytest.mark.parametrize(
    "filters",
    [
        {"patient_id = 'x' OR 1": "1"},
        {"region; DROP TABLE dim_patients": "x"},
        {"region": {"nested": "dict"}},
        {"region": None},
    ],
)
def test_invalid_filters_are_refused_before_any_query(live_workspace, filters):
    result = query_gold_table("dim_patients", filters)

    assert result.is_error is True
    assert live_workspace == []


def test_unknown_local_column_is_a_tool_error_not_a_crash(tmp_path):
    result = query_gold_table(
        "dim_patients", {"no_such_column": "x"}, db_path=tmp_path / "gold.duckdb", seed_sql_path=SEED_SQL
    )

    assert result.is_error is True
    assert "query failed" in json.loads(result.content)["error"]


def test_live_statement_failure_is_a_tool_error(monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", "https://example.gcp.databricks.com")
    monkeypatch.setenv("DATABRICKS_TOKEN", "test-token")
    monkeypatch.setenv("DATABRICKS_WAREHOUSE_ID", "wh-123")
    monkeypatch.delenv("GCP_PROJECT_ID", raising=False)

    def handler(request):
        return httpx.Response(
            200, json={"status": {"state": "FAILED", "error": {"message": "INSUFFICIENT_PERMISSIONS"}}}
        )

    monkeypatch.setattr(
        data_query,
        "DatabricksClient",
        lambda config: DatabricksClient(config, transport=httpx.MockTransport(handler)),
    )

    result = query_gold_table("dim_patients")

    assert result.is_error is True
    assert "INSUFFICIENT_PERMISSIONS" in json.loads(result.content)["error"]


class _NoopClaude:
    def run_tool_loop(self, system, messages, tools, dispatch, max_turns=6):
        return "noop"


def test_orchestrator_masks_raw_phi_returned_by_the_live_path(live_workspace, tmp_path):
    orchestrator = OrchestratorAgent(claude=_NoopClaude(), audit_log_path=tmp_path / "audit.jsonl")

    result = orchestrator._dispatch("query_gold_table", {"table": "dim_patients"})

    assert len(live_workspace) == 1
    assert "Jane Alvarez" not in result.content
    assert "MRN-000123" not in result.content
    row = json.loads(result.content)[0]
    assert row["full_name"] == "***REDACTED***"
    assert row["mrn"] == "***REDACTED***"
    assert "Jane Alvarez" not in (tmp_path / "audit.jsonl").read_text()
