"""NEWS2 scoring (agent/tools/early_warning.py) against the published bands,
and the simulator's physiology (simulator/physiology.py): correlated,
patient-specific vitals and episodes that move several vitals together."""

import random
import statistics

import pytest

from agent.tools.early_warning import news2, parameter_scores
from simulator.physiology import EPISODES, Physiology

NORMAL = {"resp_rate": 16, "spo2": 97, "sbp": 122, "heart_rate": 74, "temp_c": 36.8, "dbp": 78}


@pytest.mark.parametrize(
    ("name", "value", "points"),
    [
        ("resp_rate", 8, 3),
        ("resp_rate", 9, 1),
        ("resp_rate", 20, 0),
        ("resp_rate", 21, 2),
        ("resp_rate", 25, 3),
        ("spo2", 91, 3),
        ("spo2", 92, 2),
        ("spo2", 95, 1),
        ("spo2", 96, 0),
        ("sbp", 90, 3),
        ("sbp", 100, 2),
        ("sbp", 110, 1),
        ("sbp", 219, 0),
        ("sbp", 220, 3),
        ("heart_rate", 40, 3),
        ("heart_rate", 50, 1),
        ("heart_rate", 90, 0),
        ("heart_rate", 110, 1),
        ("heart_rate", 130, 2),
        ("heart_rate", 131, 3),
        ("temp_c", 35.0, 3),
        ("temp_c", 36.0, 1),
        ("temp_c", 38.0, 0),
        ("temp_c", 39.0, 1),
        ("temp_c", 39.1, 2),
    ],
)
def test_news2_parameter_bands(name, value, points):
    assert parameter_scores(NORMAL | {name: value})[name] == points


def test_normal_panel_scores_zero_and_does_not_trigger():
    result = news2(NORMAL)
    assert (result["score"], result["risk"], result["trigger"]) == (0, "low", False)


def test_single_red_parameter_triggers_even_with_low_total():
    result = news2(NORMAL | {"spo2": 90})
    assert result["score"] == 3
    assert result["single_parameter_3"] == ["spo2"]
    assert (result["risk"], result["trigger"]) == ("low-medium", True)


def test_aggregate_of_seven_is_high_risk():
    result = news2(NORMAL | {"resp_rate": 23, "heart_rate": 115, "sbp": 105, "temp_c": 38.5, "spo2": 94})
    assert result["score"] == 2 + 2 + 1 + 1 + 1
    assert result["risk"] == "high"


def _stable_panels(n, unit="MedSurg", seed=1):
    rng = random.Random(seed)
    return [Physiology.new(unit, rng).panel(rng) for _ in range(n)]


def test_blood_pressure_is_correlated_like_in_people():
    panels = _stable_panels(2000)
    sbp = [p["sbp"] for p in panels]
    dbp = [p["dbp"] for p in panels]
    assert statistics.correlation(sbp, dbp) > 0.6


def test_a_patient_is_consistent_over_time():
    rng = random.Random(2)
    a, b = Physiology.new("MedSurg", rng), Physiology.new("MedSurg", rng)
    a_hr = [a.panel(rng)["heart_rate"] for _ in range(50)]
    b_hr = [b.panel(rng)["heart_rate"] for _ in range(50)]
    # Reading-to-reading noise is small next to the difference between people.
    assert statistics.pstdev(a_hr) < 5
    assert abs(statistics.mean(a_hr) - statistics.mean(b_hr)) == pytest.approx(
        abs(a.baseline["heart_rate"] - b.baseline["heart_rate"]), abs=2.5
    )


def test_icu_patients_are_sicker_than_medsurg():
    icu = statistics.mean(p["heart_rate"] for p in _stable_panels(2000, "ICU"))
    medsurg = statistics.mean(p["heart_rate"] for p in _stable_panels(2000, "MedSurg"))
    assert icu - medsurg > 4


@pytest.mark.parametrize("episode", sorted(EPISODES))
def test_episodes_move_several_vitals_together(episode):
    rng = random.Random(3)
    patient = Physiology.new("MedSurg", rng)
    before = patient.expected()
    patient.state, patient.severity = episode, 1.0
    after = patient.expected()
    moved = [k for k in before if abs(after[k] - before[k]) > 1.0]
    assert len(moved) >= 2


def test_episodes_start_ramp_and_resolve():
    rng = random.Random(4)
    patient = Physiology.new("ICU", rng)
    seen = set()
    for _ in range(20000):
        patient.step(1.0, rng)
        seen.add(patient.state)
        if patient.state != "stable":
            assert 0.0 <= patient.severity <= 1.0
    assert "stable" in seen and len(seen) > 2  # episodes happened and ended


def test_full_severity_episodes_mostly_trigger_news2():
    rng = random.Random(5)
    for episode in ("respiratory_distress", "sepsis", "shock"):
        triggered = 0
        for _ in range(200):
            patient = Physiology.new("MedSurg", rng)
            patient.state, patient.severity = episode, 1.0
            triggered += news2(patient.panel(rng))["trigger"]
        assert triggered / 200 > 0.8, episode
