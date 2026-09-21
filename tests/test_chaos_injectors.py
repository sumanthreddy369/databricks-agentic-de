from datetime import UTC, datetime

import pytest

from common.contracts import VITALS_WATERMARK_MINUTES
from simulator.chaos import (
    build_late_vitals_payload,
    build_malformed_record,
    build_prompt_injection_payload,
    build_schema_drift_payload,
)


def test_schema_drift_payload_renames_value_key():
    payload = build_schema_drift_payload()
    assert "val" in payload["vital"]
    assert "value" not in payload["vital"]


def test_late_vitals_payload_is_older_than_watermark():
    payload = build_late_vitals_payload()
    event_ts = datetime.fromisoformat(payload["event_ts"])
    age_minutes = (datetime.now(UTC) - event_ts).total_seconds() / 60
    assert age_minutes > VITALS_WATERMARK_MINUTES


def test_malformed_record_is_not_valid_json():
    import json

    raw = build_malformed_record()
    assert isinstance(raw, bytes)
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)


def test_prompt_injection_payload_targets_mrn_and_name():
    payload = build_prompt_injection_payload()
    notes = payload["notes"].lower()
    assert "ignore previous instructions" in notes
    assert "mrn" in notes and "name" in notes
