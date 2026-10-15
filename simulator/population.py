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
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from common.contracts import VITAL_ITEM_TYPES
from common.identity import NICKNAMES
from simulator.domain import (
    _LAST_NAMES,
    EncounterInfo,
    PatientEvent,
    PatientSnapshot,
    generate_admitted_event,
    generate_discharged_event,
    generate_patient_snapshot,
    generate_transfer_event,
    generate_vitals_event,
)
from simulator.physiology import Physiology

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
    # Per-patient physiology: readings move together and with the patient's state.
    physiology: Physiology | None = None
    last_step: datetime | None = None


def _jittered_next_due(now: datetime) -> datetime:
    return now + timedelta(seconds=random.uniform(*VITAL_JITTER_SECONDS))


def _vary(patient: PatientSnapshot, rng) -> PatientSnapshot:
    """A registration-desk difference: a typo or transposition in the name, or
    day and month swapped in the birth date (when the swap is a valid date)."""
    year, month, day = patient.birth_date.split("-")
    if rng.random() < 0.3 and int(day) <= 12 and day != month:
        return replace(patient, birth_date=f"{year}-{day}-{month}")
    name = list(patient.full_name)
    letters = [i for i, c in enumerate(name[:-1]) if c.isalpha() and name[i + 1].isalpha()]
    i = rng.choice(letters)
    if rng.random() < 0.5:
        name[i], name[i + 1] = name[i + 1], name[i]  # transposition
    else:
        name[i] = rng.choice([c for c in "aeiourstln" if c != name[i].lower()])  # one-letter typo
    return replace(patient, full_name="".join(name))


def _rename(patient: PatientSnapshot, rng) -> PatientSnapshot:
    """A name that changed outright, beyond a typo: registered under a
    nickname, or under a new surname (marriage). Birth date and gender stay."""
    first, last = patient.full_name.split(" ", 1)
    if rng.random() < 0.5:
        return replace(patient, full_name=f"{rng.choice(NICKNAMES[first.lower()]).title()} {last}")
    return replace(patient, full_name=f"{first} {rng.choice([n for n in _LAST_NAMES if n != last])}")


class Population:
    def __init__(
        self,
        target_size: int = 1000,
        rng: random.Random | None = None,
        identity_collision_rate: float = 0.0,
        duplicate_person_rate: float = 0.0,
        duplicate_variant_rate: float = 0.0,
        duplicate_rename_rate: float = 0.0,
    ) -> None:
        """The two real-world identity defects (problem catalog M2), injected
        on purpose so the Silver checks can be measured; both 0 by default:
        - `identity_collision_rate`: a new admission is a different person
          given an already-issued patient_id (two people, one id);
        - `duplicate_person_rate`: a new admission is a returning person
          registered under a new patient_id and MRN (one person, two ids);
        - `duplicate_variant_rate`: share of those duplicates registered with a
          small difference - a one-letter typo or transposition in the name,
          or day and month swapped - the cases exact matching misses;
        - `duplicate_rename_rate`: share of the remaining duplicates registered
          under a nickname or a new surname - the cases edit distance misses.
        Every injected case is listed in `injected_identity` (ground truth for
        scoring the Silver checks; never part of the event stream)."""
        self.target_size = target_size
        self._rng = rng or random
        self.active: dict[str, ActiveEncounter] = {}  # keyed by encounter_id
        self.identity_collision_rate = identity_collision_rate
        self.issued_patient_ids: list[str] = []
        self.identity_collisions = 0
        self.duplicate_person_rate = duplicate_person_rate
        self.issued_people: list[PatientSnapshot] = []
        self.duplicate_people = 0
        self.duplicate_variant_rate = duplicate_variant_rate
        self.duplicate_rename_rate = duplicate_rename_rate
        self.injected_identity: list[dict] = []

    def admit_new_patients(self, count: int, now: datetime) -> list[PatientEvent]:
        events: list[PatientEvent] = []
        for _ in range(count):
            if self.issued_patient_ids and self._rng.random() < self.identity_collision_rate:
                patient_id = self._rng.choice(self.issued_patient_ids)
                self.identity_collisions += 1
                self.injected_identity.append({"kind": "two_people_one_id", "patient_ids": [patient_id]})
            else:
                patient_id = f"pt_{self._rng.getrandbits(32):08x}"
                self.issued_patient_ids.append(patient_id)
            patient = generate_patient_snapshot(patient_id)
            if self.issued_people and self._rng.random() < self.duplicate_person_rate:
                person = self._rng.choice(self.issued_people)
                patient = replace(
                    patient, **{k: getattr(person, k) for k in ("full_name", "birth_date", "gender", "region")}
                )
                kind = "one_person_two_ids"
                if self._rng.random() < self.duplicate_variant_rate:
                    patient, kind = _vary(patient, self._rng), "one_person_two_ids_variant"
                # Only draws when enabled, so feeds generated without it are unchanged.
                elif self.duplicate_rename_rate and self._rng.random() < self.duplicate_rename_rate:
                    patient, kind = _rename(patient, self._rng), "one_person_two_ids_renamed"
                self.duplicate_people += 1
                self.injected_identity.append(
                    {"kind": kind, "patient_ids": sorted([person.patient_id, patient.patient_id])}
                )
            else:
                self.issued_people.append(patient)
            encounter_id = f"enc_{uuid.uuid4().hex[:8]}"
            event = generate_admitted_event(patient, encounter_id)
            events.append(event)
            self.active[encounter_id] = ActiveEncounter(
                patient=patient,
                encounter=event.encounter,
                next_vital_due={item: _jittered_next_due(now) for item in VITAL_ITEM_TYPES},
                physiology=Physiology.new(event.encounter.unit, self._rng),
                last_step=now,
            )
        return events

    def tick(self, now: datetime) -> list[PatientEvent]:
        events: list[PatientEvent] = []
        discharged_encounter_ids = []

        for encounter_id, active in list(self.active.items()):
            if active.physiology is not None and active.last_step is not None:
                active.physiology.step((now - active.last_step).total_seconds() / 60, self._rng)
                active.last_step = now
            for itemid, due_at in list(active.next_vital_due.items()):
                if due_at <= now:
                    value = active.physiology.reading(itemid, self._rng) if active.physiology else None
                    events.append(generate_vitals_event(active.patient.patient_id, encounter_id, itemid, value))
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
                if active.physiology is not None:
                    active.physiology.set_unit(to_unit)

        for encounter_id in discharged_encounter_ids:
            del self.active[encounter_id]

        # Backfill discharges (and top up to target_size generally) with
        # fresh admissions so the pool size stays roughly constant.
        shortfall = max(0, self.target_size - len(self.active))
        if shortfall:
            events.extend(self.admit_new_patients(shortfall, now))

        return events
