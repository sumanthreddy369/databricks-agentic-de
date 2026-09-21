"""Pure event/entity generators for the patient_events simulator.

Entity shapes are modeled on Synthea's FHIR resources (Patient, Encounter) —
https://synthea.mitre.org — and the vitals-over-time structure is modeled on
MIMIC-IV's `chartevents` table (`subject_id, charttime, itemid, value,
valueuom`) — Johnson et al., MIMIC-IV, PhysioNet. No real Synthea-generated
files or MIMIC-IV data are used, downloaded, or required here. Vital-sign
ranges (common.contracts.VITAL_RANGES) are original illustrative
approximations, not derived from either dataset's real values.

To swap in real Synthea CSVs or a real MIMIC-IV extract for higher fidelity,
replace these generator functions with a file reader over the same schema —
simulator/population.py, simulator/producer.py, and the downstream pipeline
are unchanged, since they only depend on the PatientEvent shape, not on how
it's produced.
"""

import random
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from common.contracts import VITAL_RANGES

_REGIONS = ("northeast", "midwest", "south", "west")
_GENDERS = ("female", "male", "nonbinary")
_UNITS = ("ED", "ICU", "MedSurg", "Cardiology", "Oncology")
_FIRST_NAMES = ("James", "Mary", "Robert", "Patricia", "Linda", "Michael", "Barbara", "Jennifer", "David", "Susan")
_LAST_NAMES = (
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez"
)

# Probability a generated vital reading is deliberately pushed outside its
# clinically-plausible band, for realism (real vitals monitors do produce
# occasional out-of-range readings — sensor noise, genuine deterioration,
# etc.) and so downstream data-quality expectations have something to catch.
OUT_OF_RANGE_PROBABILITY = 0.03


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class PatientSnapshot:
    patient_id: str
    mrn: str
    full_name: str
    birth_date: str
    gender: str
    region: str


@dataclass
class EncounterInfo:
    encounter_id: str
    encounter_type: str
    unit: str
    attending_provider_id: str
    status: str


@dataclass
class VitalReading:
    itemid: str
    value: float
    valueuom: str


@dataclass
class PatientEvent:
    event_id: str
    event_type: str
    event_ts: datetime
    patient_id: str
    encounter_id: str
    schema_version: int = 1
    patient: PatientSnapshot | None = None
    encounter: EncounterInfo | None = None
    transfer: dict | None = None
    vital: VitalReading | None = None
    notes: str | None = None
    extra_fields: dict = field(default_factory=dict)  # for chaos payload experimentation only


_VALUEUOM_BY_ITEM = {
    "heart_rate": "bpm",
    "spo2": "%",
    "resp_rate": "breaths/min",
    "temp_c": "C",
    "sbp": "mmHg",
    "dbp": "mmHg",
}


def generate_patient_snapshot(patient_id: str | None = None) -> PatientSnapshot:
    pid = patient_id or f"pt_{random.randint(0, 99999):05d}"
    full_name = f"{random.choice(_FIRST_NAMES)} {random.choice(_LAST_NAMES)}"
    birth_year = random.randint(1935, 2020)
    return PatientSnapshot(
        patient_id=pid,
        mrn=f"MRN-{random.randint(0, 999999):06d}",
        full_name=full_name,
        birth_date=f"{birth_year:04d}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}",
        gender=random.choice(_GENDERS),
        region=random.choice(_REGIONS),
    )


def generate_admitted_event(
    patient: PatientSnapshot,
    encounter_id: str,
    unit: str | None = None,
    attending_provider_id: str | None = None,
) -> PatientEvent:
    encounter = EncounterInfo(
        encounter_id=encounter_id,
        encounter_type=random.choice(("inpatient", "outpatient", "emergency")),
        unit=unit or random.choice(_UNITS),
        attending_provider_id=attending_provider_id or f"prov_{random.randint(0, 999):03d}",
        status="in-progress",
    )
    return PatientEvent(
        event_id=str(uuid.uuid4()),
        event_type="admitted",
        event_ts=_now(),
        patient_id=patient.patient_id,
        encounter_id=encounter_id,
        patient=patient,
        encounter=encounter,
    )


def generate_vitals_event(patient_id: str, encounter_id: str, itemid: str) -> PatientEvent:
    low, high = VITAL_RANGES[itemid]
    if random.random() < OUT_OF_RANGE_PROBABILITY:
        # Push outside the band on a random side for a realistic outlier.
        span = high - low
        value = random.choice([low - random.uniform(0.1, span * 0.5), high + random.uniform(0.1, span * 0.5)])
    else:
        value = random.uniform(low, high)

    vital = VitalReading(itemid=itemid, value=round(value, 1), valueuom=_VALUEUOM_BY_ITEM[itemid])
    return PatientEvent(
        event_id=str(uuid.uuid4()),
        event_type="vitals_reading",
        event_ts=_now(),
        patient_id=patient_id,
        encounter_id=encounter_id,
        vital=vital,
    )


def generate_transfer_event(patient_id: str, encounter_id: str, from_unit: str, to_unit: str) -> PatientEvent:
    return PatientEvent(
        event_id=str(uuid.uuid4()),
        event_type="transferred",
        event_ts=_now(),
        patient_id=patient_id,
        encounter_id=encounter_id,
        transfer={"from_unit": from_unit, "to_unit": to_unit},
    )


def generate_discharged_event(patient_id: str, encounter_id: str) -> PatientEvent:
    return PatientEvent(
        event_id=str(uuid.uuid4()),
        event_type="discharged",
        event_ts=_now(),
        patient_id=patient_id,
        encounter_id=encounter_id,
    )
