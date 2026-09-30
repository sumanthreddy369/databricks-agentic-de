"""Cross-checks common/contracts.py against the files that must stay in sync
with it, without importing pyspark (pipeline/common/schemas.py is read as
text, since pyspark is an optional `databricks` extra not installed here).
"""

from pathlib import Path

from common.contracts import PATIENT_EVENT_ENVELOPE_FIELDS

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_schemas_py_mentions_every_envelope_field():
    schemas_text = (REPO_ROOT / "pipeline" / "common" / "schemas.py").read_text()
    for field_name in PATIENT_EVENT_ENVELOPE_FIELDS:
        assert f'"{field_name}"' in schemas_text, f"{field_name} missing from pipeline/common/schemas.py"


def test_masking_sql_mentions_masked_columns():
    masking_sql = (
        REPO_ROOT / "governance" / "05_unity_catalog" / "row_filters_and_masking.sql"
    ).read_text()
    assert "full_name" in masking_sql
    assert "mrn" in masking_sql


def test_silver_vitals_uses_watermark_not_apply_changes():
    silver_vitals = (REPO_ROOT / "pipeline" / "03_silver" / "silver_vitals.py").read_text()
    assert "withWatermark" in silver_vitals
    # The docstring references dlt.apply_changes only for contrast (explaining
    # why this table does NOT use it) — assert there's no actual invocation.
    assert "dlt.apply_changes(" not in silver_vitals


def test_silver_contract_gate_has_hard_stop_expectation():
    # tests/test_dlt_pipeline_graph.py additionally proves this gate reads
    # Bronze unfiltered and sits upstream of every Silver consumer.
    contract_gate = (REPO_ROOT / "pipeline" / "03_silver" / "silver_patient_events.py").read_text()
    assert "expect_or_fail" in contract_gate
    assert "known_event_type" in contract_gate
