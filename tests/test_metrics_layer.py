"""Proves the metrics layer: one definition per metric, shared by the
fallback agent (compute_metric) and Genie (governance/genie/space.json).

- the definitions are well-formed and match the data the simulator produces;
- dimension values match exactly, with synonyms ("intensive care" -> ICU)
  and refusal of unknown values ("PICU") instead of pattern widening;
- space.json is regenerated from the definitions (stale = fail);
- for every metric, the SQL Genie is given and compute_metric return the
  same number on the same data.
"""

import json

import duckdb
import pytest

from agent.orchestrator import DA_TOOLS, OrchestratorAgent
from agent.tools import data_query
from agent.tools.metrics import compute_metric, load, normalize, render_sql
from scripts.build_genie_space import SPACE_PATH, render
from simulator import autoloader_feed, domain

# Enough rows per group that nothing is small-cell suppressed (>= 11 patients).
SEED = """
CREATE TABLE fct_encounters (encounter_id VARCHAR, patient_id VARCHAR, event_ts TIMESTAMP, encounter_type VARCHAR,
    unit VARCHAR, attending_provider_id VARCHAR, status VARCHAR);
INSERT INTO fct_encounters SELECT 'e' || i, 'p' || i, TIMESTAMP '2026-10-07 02:00:00',
    CASE WHEN i % 3 = 0 THEN 'inpatient' WHEN i % 3 = 1 THEN 'emergency' ELSE 'outpatient' END,
    CASE WHEN i % 5 = 0 THEN 'ICU' WHEN i % 5 = 1 THEN 'ED' WHEN i % 5 = 2 THEN 'MedSurg'
         WHEN i % 5 = 3 THEN 'Cardiology' ELSE 'Oncology' END,
    'prov_1', CASE WHEN i % 4 = 0 THEN 'discharged' ELSE 'in-progress' END
    FROM range(400) t(i);
INSERT INTO fct_encounters SELECT 'picu' || i, 'pp' || i, TIMESTAMP '2026-10-07 02:00:00', 'inpatient', 'PICU',
    'prov_1', 'in-progress' FROM range(30) t(i);

CREATE TABLE fct_encounter_history (encounter_id VARCHAR, patient_id VARCHAR, encounter_type VARCHAR, unit VARCHAR,
    attending_provider_id VARCHAR, status VARCHAR, valid_from TIMESTAMP, valid_to TIMESTAMP);
INSERT INTO fct_encounter_history SELECT 'h' || i, 'hp' || i, 'emergency', 'ED', 'prov_1', 'in-progress',
    TIMESTAMP '2026-10-07 02:00:00', TIMESTAMP '2026-10-07 02:40:00' FROM range(40) t(i);
INSERT INTO fct_encounter_history SELECT 'h' || i, 'hp' || i, 'emergency', 'ICU', 'prov_1', 'in-progress',
    TIMESTAMP '2026-10-07 02:40:00', NULL FROM range(40) t(i);

CREATE TABLE gold_live_vitals_by_unit (window_start TIMESTAMP, window_end TIMESTAMP, unit VARCHAR,
    avg_heart_rate DOUBLE, out_of_range_count BIGINT, reading_count BIGINT);
INSERT INTO gold_live_vitals_by_unit VALUES
    (TIMESTAMP '2026-10-07 02:15:00', TIMESTAMP '2026-10-07 02:30:00', 'ICU', 90.0, 30, 3000),
    (TIMESTAMP '2026-10-07 02:30:00', TIMESTAMP '2026-10-07 02:45:00', 'ICU', 120.0, 2, 30),
    (TIMESTAMP '2026-10-07 02:15:00', TIMESTAMP '2026-10-07 02:30:00', 'ED', 80.0, 10, 500);

CREATE TABLE dim_providers (provider_id VARCHAR, full_name VARCHAR, specialty VARCHAR, npi VARCHAR,
    home_unit VARCHAR, updated_at TIMESTAMP);
INSERT INTO dim_providers SELECT 'pr' || i, 'Dr X', CASE WHEN i % 2 = 0 THEN 'Cardiology' ELSE 'Oncology' END, '1',
    'ICU', TIMESTAMP '2026-10-07 02:00:00' FROM range(30) t(i);

CREATE TABLE dim_patients (patient_id VARCHAR, event_ts TIMESTAMP, mrn VARCHAR, full_name VARCHAR,
    birth_date VARCHAR, gender VARCHAR, region VARCHAR);
INSERT INTO dim_patients SELECT 'p' || i, TIMESTAMP '2026-10-07 02:00:00', 'MRN-' || i, 'Name ' || i, '2000-01-01',
    'female', CASE WHEN i % 2 = 0 THEN 'south' ELSE 'west' END FROM range(60) t(i);
"""


@pytest.fixture
def db(tmp_path):
    seed = tmp_path / "seed.sql"
    seed.write_text(SEED)
    return {"db_path": tmp_path / "gold.duckdb", "seed_sql_path": seed}


def _payload(result):
    assert result.is_error is False, result.content
    return json.loads(result.content)


# --- definitions ----------------------------------------------------------


def test_every_metric_is_well_formed():
    layer = load()
    for metric in layer["metrics"].values():
        assert metric["table"] in data_query._ALLOWED_TABLES, metric["id"]
        assert metric["measure"]["type"] in {"count", "count_distinct", "sum", "weighted_avg"}, metric["id"]
        for dim in metric.get("dimensions", []):
            assert dim in layer["dimensions"], (metric["id"], dim)
        for key in ("name", "description", "example_question"):
            assert metric.get(key), (metric["id"], key)


def test_dimension_values_match_what_the_simulator_produces():
    dims = load()["dimensions"]
    assert sorted(dims["unit"]["values"]) == sorted(domain._UNITS)
    assert sorted(dims["home_unit"]["values"]) == sorted(autoloader_feed._UNITS)
    assert sorted(dims["specialty"]["values"]) == sorted(autoloader_feed._SPECIALTIES)
    assert sorted(dims["region"]["values"]) == sorted(domain._REGIONS)
    assert sorted(dims["gender"]["values"]) == sorted(domain._GENDERS)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("ICU", "ICU"), ("icu", "ICU"), ("intensive care", "ICU"), ("ER", "ED"), ("med-surg", "MedSurg")],
)
def test_unit_values_normalize_to_exact_canonical_values(value, expected):
    assert normalize("unit", value, "fct_encounters") == expected


@pytest.mark.parametrize("value", ["PICU", "NICU", "%ICU%", "ICU 2"])
def test_unknown_unit_values_are_refused_not_widened(value):
    with pytest.raises(ValueError, match="allowed values"):
        normalize("unit", value, "fct_encounters")


def test_unassigned_is_only_valid_for_the_vitals_rollup():
    assert normalize("unit", "UNASSIGNED", "gold_live_vitals_by_unit") == "UNASSIGNED"
    with pytest.raises(ValueError):
        normalize("unit", "UNASSIGNED", "fct_encounters")


# --- Genie gets the same definitions --------------------------------------


def test_genie_space_json_is_generated_from_the_metrics_layer():
    assert SPACE_PATH.read_text(encoding="utf-8") == render(), "run: uv run python scripts/build_genie_space.py"


def test_genie_instructions_forbid_pattern_matching_on_units():
    space = json.loads(render())["space"]
    text = "".join(space["instructions"]["text_instructions"][0]["content"])
    assert "never LIKE / ILIKE" in text
    assert "unit: Cardiology, ED, ICU, MedSurg, Oncology" in text
    for example in space["instructions"]["example_question_sqls"]:
        assert "LIKE" not in "".join(example["sql"]).upper()


# --- same number on both sides --------------------------------------------


CASES = [
    ("current_census", None, {"unit": "ICU"}, None),
    ("current_census", ["unit"], None, None),
    ("census_at_time", None, {"unit": "ED"}, "2026-10-07 02:25:00"),
    ("census_at_time", ["unit"], None, "2026-10-07 02:50:00"),
    ("discharged_encounters", ["unit"], None, None),
    ("avg_heart_rate", ["unit"], None, None),
    ("out_of_range_heart_rate_readings", None, {"unit": "ICU"}, None),
    ("heart_rate_readings", ["unit"], None, None),
    ("provider_count", ["specialty"], None, None),
    ("patient_count", ["region"], None, None),
]


@pytest.mark.parametrize(("metric", "group_by", "filters", "as_of"), CASES)
def test_genie_sql_and_compute_metric_agree(db, metric, group_by, filters, as_of):
    ours = _payload(compute_metric(metric, group_by=group_by, filters=filters, as_of=as_of, **db))["rows"]

    conn = duckdb.connect(str(db["db_path"]))
    try:
        cursor = conn.execute(render_sql(metric, group_by=group_by, filters=filters, as_of=as_of, qualified=False))
        cols = [c[0] for c in cursor.description]
        genie = [dict(zip(cols, row, strict=True)) for row in cursor.fetchall()]
    finally:
        conn.close()

    assert len(ours) == len(genie)
    for mine, theirs in zip(ours, genie, strict=True):
        assert not mine.get("suppressed"), mine
        assert mine["value"] == pytest.approx(theirs["value"])
        for dim in group_by or []:
            assert mine[dim] == theirs[dim]


def test_icu_census_excludes_picu(db):
    rows = _payload(compute_metric("current_census", filters={"unit": "ICU"}, **db))["rows"]
    # 80 ICU encounters in the seed, 60 in-progress; the 30 PICU ones never count.
    assert rows == [{"value": 60}]


def test_weighted_average_is_not_the_average_of_averages(db):
    rows = _payload(compute_metric("avg_heart_rate", filters={"unit": "ICU"}, **db))["rows"]
    # (90*3000 + 120*30) / 3030 = 90.30 - not (90 + 120) / 2 = 105.
    assert rows[0]["value"] == pytest.approx(90.297, abs=0.01)


# --- refusals ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"metric": "bed_turnover"}, "unknown metric"),
        ({"metric": "current_census", "group_by": ["region"]}, "can be grouped by"),
        ({"metric": "current_census", "filters": {"unit": "PICU"}}, "allowed values"),
        ({"metric": "current_census", "as_of": "2026-10-07 02:00:00"}, "use census_at_time"),
        ({"metric": "census_at_time"}, "needs as_of"),
        ({"metric": "provider_count", "since": "2026-10-07"}, "no time column"),
    ],
)
def test_requests_outside_the_definitions_are_refused(db, kwargs, message):
    result = compute_metric(**kwargs, **db)
    assert result.is_error is True
    assert message in json.loads(result.content)["error"]


# --- agent wiring -----------------------------------------------------------


class _NoopClaude:
    def run_tool_loop(self, system, messages, tools, dispatch, max_turns=6):
        return "noop"


def test_compute_metric_is_offered_and_dispatched(db, tmp_path):
    assert "compute_metric" in {t["name"] for t in DA_TOOLS}
    orchestrator = OrchestratorAgent(
        claude=_NoopClaude(),
        duckdb_path=db["db_path"],
        seed_sql_path=db["seed_sql_path"],
        audit_log_path=tmp_path / "audit.jsonl",
    )

    result = orchestrator._dispatch(
        "compute_metric", {"metric": "current_census", "filters": {"unit": "intensive care"}}
    )

    payload = json.loads(result.content)
    assert payload["rows"] == [{"value": 60}]
    assert payload["applied_filters"] == {"status": "in-progress", "unit": "ICU"}
    assert payload["definition"]["name"] == "Current census"
