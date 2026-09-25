"""Proves the Pydantic envelope models in common/contracts.py behave as a
real runtime schema: valid events construct cleanly, malformed ones are
rejected, and an out-of-range vital is flagged rather than rejected (mirroring
the DLT `expect` vs `expect_or_drop` distinction the simulator's own
out-of-range tail — simulator.domain.OUT_OF_RANGE_PROBABILITY — depends on).
"""

import pytest
from pydantic import ValidationError

from common.contracts import (
    ALLOWED_EVENT_TYPES,
    VITAL_ITEM_TYPES,
    VITAL_RANGES,
    EncounterInfo,
    PatientEvent,
    PatientInfo,
    TransferInfo,
    VitalReading,
)

VALID_TS = "2026-09-21T14:03:11Z"


def test_valid_admitted_event_constructs_cleanly():
    event = PatientEvent(
        event_id="evt-1",
        event_type="admitted",
        event_ts=VALID_TS,
        patient_id="pt_00001",
        encounter_id="enc_00001",
        patient=PatientInfo(
            mrn="MRN-000123",
            full_name="Jane Alvarez",
            birth_date="1968-04-02",
            gender="female",
            region="midwest",
        ),
        encounter=EncounterInfo(
            encounter_type="inpatient", unit="ICU", attending_provider_id="prov_042", status="in-progress"
        ),
    )
    assert event.event_type == "admitted"
    assert event.patient.mrn == "MRN-000123"


def test_valid_vitals_event_constructs_cleanly():
    event = PatientEvent(
        event_id="evt-2",
        event_type="vitals_reading",
        event_ts=VALID_TS,
        patient_id="pt_00001",
        encounter_id="enc_00001",
        vital=VitalReading(itemid="heart_rate", value=88.0, valueuom="bpm"),
    )
    assert event.vital.itemid == "heart_rate"
    assert event.vital.out_of_range is False


def test_valid_transfer_event_constructs_cleanly():
    event = PatientEvent(
        event_id="evt-3",
        event_type="transferred",
        event_ts=VALID_TS,
        patient_id="pt_00001",
        encounter_id="enc_00001",
        transfer=TransferInfo(from_unit="ED", to_unit="ICU"),
    )
    assert event.transfer.to_unit == "ICU"


def test_all_allowed_event_types_are_accepted():
    for event_type in ALLOWED_EVENT_TYPES:
        event = PatientEvent(
            event_id="evt-x",
            event_type=event_type,
            event_ts=VALID_TS,
            patient_id="pt_00001",
            encounter_id="enc_00001",
        )
        assert event.event_type == event_type


def test_all_vital_item_types_are_accepted():
    for itemid in VITAL_ITEM_TYPES:
        low, high = VITAL_RANGES[itemid]
        mid = (low + high) / 2
        vital = VitalReading(itemid=itemid, value=mid, valueuom="unit")
        assert vital.itemid == itemid
        assert vital.out_of_range is False


def test_unknown_event_type_is_rejected():
    with pytest.raises(ValidationError):
        PatientEvent(
            event_id="evt-bad",
            event_type="not_a_real_event_type",
            event_ts=VALID_TS,
            patient_id="pt_00001",
            encounter_id="enc_00001",
        )


def test_unknown_vital_itemid_is_rejected():
    with pytest.raises(ValidationError):
        VitalReading(itemid="not_a_real_vital", value=88.0, valueuom="bpm")


def test_missing_required_field_is_rejected():
    with pytest.raises(ValidationError):
        PatientEvent(
            event_type="admitted",
            event_ts=VALID_TS,
            patient_id="pt_00001",
            encounter_id="enc_00001",
        )  # missing event_id


def test_unexpected_top_level_field_is_rejected():
    with pytest.raises(ValidationError):
        PatientEvent(
            event_id="evt-drift",
            event_type="admitted",
            event_ts=VALID_TS,
            patient_id="pt_00001",
            encounter_id="enc_00001",
            some_unexpected_field="uh-oh",
        )


def test_out_of_range_vital_is_flagged_not_rejected():
    """The simulator deliberately produces a small out-of-range tail for
    realism (see simulator/domain.py's OUT_OF_RANGE_PROBABILITY) — the
    Pydantic model must not reject those readings, only flag them, mirroring
    dlt.expect (log-and-keep) rather than expect_or_drop/expect_or_fail.
    """
    low, high = VITAL_RANGES["heart_rate"]
    vital = VitalReading(itemid="heart_rate", value=high + 50, valueuom="bpm")
    assert vital.value == high + 50  # construction succeeded, value preserved
    assert vital.out_of_range is True

    vital_low = VitalReading(itemid="heart_rate", value=max(low - 50, 0.1), valueuom="bpm")
    assert vital_low.out_of_range is True


def test_in_range_vital_is_not_flagged():
    low, high = VITAL_RANGES["spo2"]
    vital = VitalReading(itemid="spo2", value=(low + high) / 2, valueuom="%")
    assert vital.out_of_range is False
