"""Proves agent/tools/data_query.py:aggregate_gold_table — counts computed
in SQL rather than from capped rows, small-cell suppression, point-in-time
(as_of) answers over encounter history, and the same column guardrails as
query_gold_table — against DuckDB, plus the request shape on the mocked
live path. Zero network calls.
"""

import json

import httpx
import pytest

from agent.databricks_client import DatabricksClient
from agent.orchestrator import OrchestratorAgent
from agent.tools import data_query
from agent.tools.data_query import aggregate_gold_table, query_gold_table
from common.contracts import MAX_TOOL_RESULT_ROWS, MIN_CELL_SIZE

# 600 active ICU encounters (more than the 500-row cap), 3 in the Burn Unit
# (a small cell), 12 discharged from Cardiology, and an encounter history
# with an ED -> ICU transfer at 10:30.
SEED = """
CREATE TABLE fct_encounters (encounter_id VARCHAR, patient_id VARCHAR, encounter_type VARCHAR,
                             unit VARCHAR, attending_provider_id VARCHAR, status VARCHAR);
INSERT INTO fct_encounters SELECT 'enc_icu_' || i, 'pt_icu_' || i, 'inpatient', 'ICU', 'prov_042', 'in-progress'
    FROM range(600) t(i);
INSERT INTO fct_encounters SELECT 'enc_burn_' || i, 'pt_burn_' || i, 'inpatient', 'Burn Unit', 'prov_017',
    'in-progress'
    FROM range(3) t(i);
INSERT INTO fct_encounters SELECT 'enc_card_' || i, 'pt_card_' || i, 'inpatient', 'Cardiology', 'prov_017',
    'discharged'
    FROM range(12) t(i);

CREATE TABLE dim_patients (patient_id VARCHAR, mrn VARCHAR, full_name VARCHAR, birth_date VARCHAR,
                           gender VARCHAR, region VARCHAR);
INSERT INTO dim_patients VALUES ('pt_1', 'MRN-1', 'Jane Alvarez', '1968-04-02', 'female', 'midwest');

CREATE TABLE fct_vitals (event_id VARCHAR, encounter_id VARCHAR, patient_id VARCHAR, itemid VARCHAR,
                         value DOUBLE, valueuom VARCHAR, event_ts TIMESTAMP);
INSERT INTO fct_vitals SELECT 'evt_' || i, 'enc_icu_' || (i % 20), 'pt_icu_' || (i % 20), 'heart_rate',
    80 + (i % 20), 'bpm', TIMESTAMP '2026-09-21 14:00:00' + INTERVAL (i) MINUTE FROM range(100) t(i);
INSERT INTO fct_vitals SELECT 'evt_one_' || i, 'enc_burn_0', 'pt_burn_0', 'heart_rate', 120, 'bpm',
    TIMESTAMP '2026-09-21 14:00:00' FROM range(50) t(i);

CREATE TABLE fct_encounter_history (encounter_id VARCHAR, patient_id VARCHAR, encounter_type VARCHAR,
    unit VARCHAR, attending_provider_id VARCHAR, status VARCHAR, valid_from TIMESTAMP, valid_to TIMESTAMP);
INSERT INTO fct_encounter_history SELECT 'enc_h_' || i, 'pt_h_' || i, 'inpatient', 'ED', 'prov_042', 'in-progress',
    TIMESTAMP '2026-09-21 08:00:00', TIMESTAMP '2026-09-21 10:30:00' FROM range(15) t(i);
INSERT INTO fct_encounter_history SELECT 'enc_h_' || i, 'pt_h_' || i, 'inpatient', 'ICU', 'prov_042',
    'in-progress',
    TIMESTAMP '2026-09-21 10:30:00', NULL FROM range(15) t(i);
"""


@pytest.fixture
def db(tmp_path):
    seed = tmp_path / "seed.sql"
    seed.write_text(SEED)
    return {"db_path": tmp_path / "gold.duckdb", "seed_sql_path": seed}


def _payload(result):
    assert result.is_error is False, result.content
    return json.loads(result.content)


def test_count_is_exact_beyond_the_row_cap(db):
    rows = json.loads(query_gold_table("fct_encounters", {"unit": "ICU"}, **db).content)
    assert len(rows) == MAX_TOOL_RESULT_ROWS + 1  # capped: counting these would be wrong

    payload = _payload(aggregate_gold_table("fct_encounters", "count", filters={"unit": "ICU"}, **db))

    assert payload["rows"] == [{"value": 600}]
    assert payload["truncated"] is False


def test_small_groups_are_suppressed_without_revealing_their_size(db):
    payload = _payload(
        aggregate_gold_table("fct_encounters", "count", filters={"status": "in-progress"}, group_by=["unit"], **db)
    )

    by_unit = {row["unit"]: row for row in payload["rows"]}
    assert by_unit["ICU"] == {"unit": "ICU", "value": 600}
    assert by_unit["Burn Unit"] == {"unit": "Burn Unit", "value": None, "suppressed": True}
    assert payload["suppressed_groups"] == 1
    assert payload["min_cell_size"] == MIN_CELL_SIZE
    # The internal group size is never returned, for any row.
    assert all("_cell_size" not in row for row in payload["rows"])


def test_suppression_counts_patients_not_readings(db):
    # 50 readings, all from one Burn Unit patient: still one patient, so the
    # average would describe that person and must be suppressed.
    payload = _payload(
        aggregate_gold_table("fct_vitals", "avg", column="value", filters={"patient_id": "pt_burn_0"}, **db)
    )

    assert payload["rows"] == [{"value": None, "suppressed": True}]


def test_zero_count_is_reported_not_suppressed(db):
    payload = _payload(aggregate_gold_table("fct_encounters", "count", filters={"unit": "Neonatal"}, **db))

    assert payload["rows"] == [{"value": 0}]


def test_average_with_range_filter_on_timestamps(db):
    payload = _payload(
        aggregate_gold_table(
            "fct_vitals",
            "avg",
            column="value",
            where=[
                {"column": "event_ts", "op": ">=", "value": "2026-09-21 14:00:00"},
                {"column": "event_ts", "op": "<", "value": "2026-09-21 15:40:00"},
                {"column": "patient_id", "op": "!=", "value": "pt_burn_0"},
            ],
            **db,
        )
    )

    assert payload["rows"][0]["value"] == pytest.approx(89.5)


def test_as_of_answers_point_in_time_census(db):
    def census(at):
        return _payload(
            aggregate_gold_table(
                "fct_encounter_history", "count_distinct", column="patient_id", group_by=["unit"], as_of=at, **db
            )
        )["rows"]

    assert census("2026-09-21 09:00:00") == [{"unit": "ED", "value": 15}]
    assert census("2026-09-21 11:00:00") == [{"unit": "ICU", "value": 15}]


def test_as_of_is_refused_on_current_state_tables(db):
    result = aggregate_gold_table("fct_encounters", "count", as_of="2026-09-21 09:00:00", **db)

    assert result.is_error is True
    assert "as_of only applies" in json.loads(result.content)["error"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"metric": "count_distinct", "column": "full_name"},
        {"metric": "count", "group_by": ["mrn"]},
        {"metric": "count", "filters": {"full_name": "Jane Alvarez"}},
        {"metric": "count", "where": [{"column": "full_name", "op": ">", "value": "J"}]},
    ],
)
def test_masked_columns_are_refused_everywhere(db, kwargs):
    result = aggregate_gold_table("dim_patients", **kwargs, **db)

    assert result.is_error is True
    assert "masked column" in json.loads(result.content)["error"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"metric": "median", "column": "value"},
        {"metric": "avg"},
        {"metric": "count", "group_by": ["unit; DROP TABLE x"]},
        {"metric": "count", "where": [{"column": "unit", "op": "LIKE", "value": "%"}]},
        {"metric": "count", "where": [{"column": "unit", "op": "=", "value": ["ICU"]}]},
        {"metric": "count", "group_by": ["a", "b", "c", "d"]},
    ],
)
def test_invalid_requests_are_refused(db, kwargs):
    assert aggregate_gold_table("fct_encounters", **kwargs, **db).is_error is True


def test_live_aggregate_sends_bound_parameters_to_gold(monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", "https://example.gcp.databricks.com")
    monkeypatch.setenv("DATABRICKS_TOKEN", "test-token")
    monkeypatch.setenv("DATABRICKS_WAREHOUSE_ID", "wh-123")
    monkeypatch.delenv("GCP_PROJECT_ID", raising=False)
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        columns = [
            {"name": "unit", "type_name": "STRING"},
            {"name": "value", "type_name": "LONG"},
            {"name": "_cell_size", "type_name": "LONG"},
        ]
        data = [["ICU", "600", "600"], ["Burn Unit", "3", "3"]]
        return httpx.Response(
            200,
            json={
                "status": {"state": "SUCCEEDED"},
                "manifest": {"schema": {"columns": columns}},
                "result": {"data_array": data},
            },
        )

    monkeypatch.setattr(
        data_query,
        "DatabricksClient",
        lambda config: DatabricksClient(config, transport=httpx.MockTransport(handler)),
    )

    payload = _payload(
        aggregate_gold_table("fct_encounter_history", "count", group_by=["unit"], as_of="2026-09-21 03:00:00")
    )

    [request] = sent
    assert request["statement"] == (
        "SELECT unit, COUNT(*) AS value, COUNT(DISTINCT patient_id) AS _cell_size "
        "FROM healthcare_agentic_de.gold.fct_encounter_history "
        "WHERE valid_from <= CAST(:p0 AS TIMESTAMP) AND (valid_to IS NULL OR valid_to > CAST(:p0 AS TIMESTAMP)) "
        f"GROUP BY unit ORDER BY unit LIMIT {MAX_TOOL_RESULT_ROWS + 1}"
    )
    assert request["parameters"] == [{"name": "p0", "value": "2026-09-21 03:00:00", "type": "STRING"}]
    assert payload["rows"] == [
        {"unit": "ICU", "value": 600},
        {"unit": "Burn Unit", "value": None, "suppressed": True},
    ]


class _NoopClaude:
    def run_tool_loop(self, system, messages, tools, dispatch, max_turns=6):
        return "noop"


def test_orchestrator_dispatches_aggregates_through_the_guardrail_pipeline(db, tmp_path):
    audit = tmp_path / "audit.jsonl"
    orchestrator = OrchestratorAgent(
        claude=_NoopClaude(), duckdb_path=db["db_path"], seed_sql_path=db["seed_sql_path"], audit_log_path=audit
    )

    result = orchestrator._dispatch(
        "aggregate_gold_table", {"table": "fct_encounters", "metric": "count", "filters": {"unit": "ICU"}}
    )

    assert json.loads(result.content)["rows"] == [{"value": 600}]
    entry = json.loads(audit.read_text().splitlines()[-1])
    assert entry["tool"] == "aggregate_gold_table"
