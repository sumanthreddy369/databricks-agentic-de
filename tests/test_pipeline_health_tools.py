import json

import pytest

from agent.tools.pipeline_health import (
    check_expectation_metrics,
    check_job_status,
    detect_schema_drift,
    notify_and_page,
    quarantine_bad_records,
    restart_pipeline,
)

BROKEN_STATE = {
    "tables": {
        "silver_vitals": {"expectations": {"plausible_vital_value": {"failure_count": 7}}},
    },
    "jobs": {
        "streaming_patient_pipeline": {"status": "FAILED"},
    },
    "schema_snapshot": {"patient_events": ["event_id", "event_type", "patient_id"]},  # missing fields
    "incidents": [],
}


@pytest.fixture
def state_path(tmp_path):
    path = tmp_path / "pipeline_state.json"
    path.write_text(json.dumps(BROKEN_STATE))
    return path


def test_check_expectation_metrics_flags_failure(state_path):
    result = check_expectation_metrics(state_path)
    payload = json.loads(result.content)
    assert payload["ok"] is False
    assert payload["failing_expectations"][0]["table"] == "silver_vitals"


def test_check_job_status_flags_failed_job(state_path):
    result = check_job_status(state_path)
    payload = json.loads(result.content)
    assert payload["ok"] is False
    assert "streaming_patient_pipeline" in payload["unhealthy"]


def test_detect_schema_drift_flags_missing_fields(state_path):
    result = detect_schema_drift(state_path)
    payload = json.loads(result.content)
    assert payload["ok"] is False
    assert "encounter_id" in payload["missing_fields"]
    assert "schema_version" in payload["missing_fields"]


def test_quarantine_bad_records_clears_failure_count(state_path):
    quarantine_bad_records(state_path, "silver_vitals", "plausible_vital_value")
    result = check_expectation_metrics(state_path)
    payload = json.loads(result.content)
    assert payload["ok"] is True


def test_restart_pipeline_sets_status_to_success(state_path):
    restart_pipeline(state_path, "streaming_patient_pipeline")
    result = check_job_status(state_path)
    payload = json.loads(result.content)
    assert payload["jobs"]["streaming_patient_pipeline"]["status"] == "SUCCESS"


def test_notify_and_page_appends_incident(state_path):
    notify_and_page(state_path, "hard-stop failure: unknown event_type")
    state = json.loads(state_path.read_text())
    assert len(state["incidents"]) == 1
    assert "unknown event_type" in state["incidents"][0]["message"]
