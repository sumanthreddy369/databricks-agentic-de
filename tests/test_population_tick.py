import random
from datetime import UTC, datetime, timedelta

from simulator.population import Population


def test_tick_only_emits_vitals_whose_due_time_has_passed():
    population = Population(target_size=5, rng=random.Random(42))
    start = datetime(2026, 1, 1, tzinfo=UTC)
    population.admit_new_patients(5, start)

    # Immediately after admission, due times are all a few seconds in the
    # future (VITAL_JITTER_SECONDS), so ticking at the same instant should
    # not fire any vitals yet.
    events = population.tick(start)
    vitals_events = [e for e in events if e.event_type == "vitals_reading"]
    assert vitals_events == []

    # Advance well past the jitter window (max 10s) — now vitals should fire.
    later = start + timedelta(seconds=30)
    events = population.tick(later)
    vitals_events = [e for e in events if e.event_type == "vitals_reading"]
    assert len(vitals_events) > 0


def test_pool_size_stays_near_target_after_many_ticks_with_discharges():
    target = 20
    population = Population(target_size=target, rng=random.Random(7))
    now = datetime(2026, 1, 1, tzinfo=UTC)
    population.admit_new_patients(target, now)

    for _ in range(200):
        now += timedelta(seconds=5)
        population.tick(now)

    # Backfilling on discharge should keep the pool within a small band of
    # the target, never drifting far off due to unbackfilled discharges.
    assert abs(len(population.active) - target) <= 2


def test_tick_never_sleeps_and_is_driven_purely_by_injected_now():
    population = Population(target_size=3, rng=random.Random(1))
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    population.admit_new_patients(3, t0)

    # Calling tick repeatedly with the SAME `now` should be idempotent with
    # respect to vitals due-time firing (no wall-clock side effects sneak in).
    first = population.tick(t0)
    second = population.tick(t0)
    assert [e.event_type for e in first if e.event_type == "vitals_reading"] == []
    assert [e.event_type for e in second if e.event_type == "vitals_reading"] == []


def test_patient_ids_are_unique_by_default():
    """The old pt_00000-pt_99999 space gave ~13 accidental collisions in a
    1,400-patient feed (two different people, one patient_id)."""
    population = Population(target_size=0, rng=random.Random(11))
    events = population.admit_new_patients(5000, datetime(2026, 1, 1, tzinfo=UTC))
    ids = [e.patient_id for e in events]
    assert len(set(ids)) == len(ids)
    assert population.identity_collisions == 0


def test_identity_collisions_are_injected_only_on_request():
    population = Population(target_size=0, rng=random.Random(12), identity_collision_rate=0.05)
    events = population.admit_new_patients(2000, datetime(2026, 1, 1, tzinfo=UTC))
    ids = [e.patient_id for e in events]
    assert population.identity_collisions > 50
    assert len(ids) - len(set(ids)) == population.identity_collisions
