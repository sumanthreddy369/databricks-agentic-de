from datetime import datetime

from common.contracts import ALLOWED_EVENT_TYPES, VITAL_ITEM_TYPES, VITAL_RANGES
from simulator.domain import (
    generate_admitted_event,
    generate_discharged_event,
    generate_patient_snapshot,
    generate_transfer_event,
    generate_vitals_event,
)

ENVELOPE_ATTRS = ("event_id", "event_type", "event_ts", "patient_id", "encounter_id", "schema_version")


def _assert_envelope(event):
    for attr in ENVELOPE_ATTRS:
        assert getattr(event, attr) is not None
    assert event.event_type in ALLOWED_EVENT_TYPES
    assert isinstance(event.event_ts, datetime)
    assert event.event_ts.tzinfo is not None  # timezone-aware


def test_admitted_event_has_full_envelope_and_nested_structs():
    patient = generate_patient_snapshot()
    event = generate_admitted_event(patient, "enc_test_001")
    _assert_envelope(event)
    assert event.event_type == "admitted"
    assert event.patient is patient
    assert event.encounter is not None
    assert event.encounter.encounter_id == "enc_test_001"


def test_vitals_event_values_usually_within_range():
    patient_id = "pt_test"
    encounter_id = "enc_test"
    out_of_range_count = 0
    total = 500
    for _ in range(total):
        event = generate_vitals_event(patient_id, encounter_id, "heart_rate")
        _assert_envelope(event)
        assert event.event_type == "vitals_reading"
        low, high = VITAL_RANGES["heart_rate"]
        if not (low <= event.vital.value <= high):
            out_of_range_count += 1

    # Documented small out-of-range tail (~3%) — allow generous slack so this
    # stays non-flaky while still catching a broken generator (e.g. one that
    # always produces out-of-range values).
    assert out_of_range_count / total < 0.15


def test_vitals_event_covers_all_known_item_types():
    for itemid in VITAL_ITEM_TYPES:
        event = generate_vitals_event("pt_test", "enc_test", itemid)
        assert event.vital.itemid == itemid


def test_transfer_event():
    event = generate_transfer_event("pt_test", "enc_test", "ED", "ICU")
    _assert_envelope(event)
    assert event.event_type == "transferred"
    assert event.transfer == {"from_unit": "ED", "to_unit": "ICU"}


def test_discharged_event():
    event = generate_discharged_event("pt_test", "enc_test")
    _assert_envelope(event)
    assert event.event_type == "discharged"
