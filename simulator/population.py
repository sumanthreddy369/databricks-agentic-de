"""Registry of currently-admitted simulated patients and the `tick()` loop
that advances them: emitting due vitals readings, and with small
probabilities transferring or discharging encounters (backfilling any
discharge immediately with a fresh admission so the pool size stays roughly
constant at a configured target).

Deliberately takes `now` as an explicit parameter everywhere rather than
reading the wall clock internally, so a caller (production loop or test) can
drive it with a real or fake clock — `tick()` itself never sleeps and has no
wall-clock dependency, which is what makes simulator/producer.py's real loop
and tests/test_population_tick.py's deterministic loop the same code path.
"""

import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from common.contracts import VITAL_ITEM_TYPES
from simulator.domain import (
    EncounterInfo,
    PatientEvent,
    PatientSnapshot,
    generate_admitted_event,
    generate_discharged_event,
    generate_patient_snapshot,
    generate_transfer_event,
    generate_vitals_event,
)

# Vitals cadence jitter, in seconds, applied every time a vital type is
# rescheduled after firing.
VITAL_JITTER_SECONDS = (3, 10)

TRANSFER_PROBABILITY_PER_TICK = 0.01
DISCHARGE_PROBABILITY_PER_TICK = 0.01

_UNITS = ("ED", "ICU", "MedSurg", "Cardiology", "Oncology")


@dataclass
class ActiveEncounter:
    patient: PatientSnapshot
    encounter: EncounterInfo
    next_vital_due: dict[str, datetime] = field(default_factory=dict)


def _jittered_next_due(now: datetime) -> datetime:
    return now + timedelta(seconds=random.uniform(*VITAL_JITTER_SECONDS))


class Population:
    def __init__(self, target_size: int = 1000, rng: random.Random | None = None) -> None:
        self.target_size = target_size
        self._rng = rng or random
        self.active: dict[str, ActiveEncounter] = {}  # keyed by encounter_id

    def admit_new_patients(self, count: int, now: datetime) -> list[PatientEvent]:
        events: list[PatientEvent] = []
        for _ in range(count):
            patient = generate_patient_snapshot()
            encounter_id = f"enc_{uuid.uuid4().hex[:8]}"
            event = generate_admitted_event(patient, encounter_id)
            events.append(event)
            self.active[encounter_id] = ActiveEncounter(
                patient=patient,
                encounter=event.encounter,
                next_vital_due={item: _jittered_next_due(now) for item in VITAL_ITEM_TYPES},
            )
        return events

    def tick(self, now: datetime) -> list[PatientEvent]:
        events: list[PatientEvent] = []
        discharged_encounter_ids = []

        for encounter_id, active in list(self.active.items()):
            for itemid, due_at in list(active.next_vital_due.items()):
                if due_at <= now:
                    events.append(generate_vitals_event(active.patient.patient_id, encounter_id, itemid))
                    active.next_vital_due[itemid] = _jittered_next_due(now)

            if self._rng.random() < DISCHARGE_PROBABILITY_PER_TICK:
                events.append(generate_discharged_event(active.patient.patient_id, encounter_id))
                discharged_encounter_ids.append(encounter_id)
                continue

            if self._rng.random() < TRANSFER_PROBABILITY_PER_TICK:
                from_unit = active.encounter.unit
                to_unit = self._rng.choice([u for u in _UNITS if u != from_unit])
                events.append(
                    generate_transfer_event(active.patient.patient_id, encounter_id, from_unit, to_unit)
                )
                active.encounter.unit = to_unit

        for encounter_id in discharged_encounter_ids:
            del self.active[encounter_id]

        # Backfill discharges (and top up to target_size generally) with
        # fresh admissions so the pool size stays roughly constant.
        shortfall = max(0, self.target_size - len(self.active))
        if shortfall:
            events.extend(self.admit_new_patients(shortfall, now))

        return events
