"""Ground truth for the identity checks (simulator) and how they're scored
(scripts/score_identity_checks.py), including the local DuckDB replica of
the Silver logic run end to end on a small simulated feed."""

import json
import random
import sys
from datetime import UTC, datetime
from pathlib import Path

from simulator.domain import PatientSnapshot
from simulator.file_feed import generate_files
from simulator.population import Population, _vary

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import score_identity_checks  # noqa: E402

PERSON = PatientSnapshot(
    patient_id="pt_00000001",
    mrn="MRN-000001",
    full_name="Mary Johnson",
    birth_date="1980-03-07",
    gender="female",
    region="west",
)


def _edits(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def test_variants_are_small_differences_a_desk_could_make():
    rng = random.Random(1)
    for _ in range(300):
        v = _vary(PERSON, rng)
        if v.birth_date != PERSON.birth_date:
            assert v.birth_date == "1980-07-03" and v.full_name == PERSON.full_name  # day and month swapped
        else:
            assert 1 <= _edits(v.full_name.lower(), PERSON.full_name.lower()) <= 2


def test_every_injected_case_is_in_the_ground_truth():
    population = Population(
        target_size=0,
        rng=random.Random(2),
        identity_collision_rate=0.02,
        duplicate_person_rate=0.05,
        duplicate_variant_rate=0.5,
    )
    population.admit_new_patients(1500, datetime(2026, 1, 1, tzinfo=UTC))
    kinds = [t["kind"] for t in population.injected_identity]
    assert kinds.count("two_people_one_id") == population.identity_collisions
    assert (
        kinds.count("one_person_two_ids") + kinds.count("one_person_two_ids_variant")
        == population.duplicate_people
    )
    assert kinds.count("one_person_two_ids_variant") > 10


def test_score_counts_each_check_against_the_truth():
    truth = [
        {"kind": "two_people_one_id", "patient_ids": ["p1"]},
        {"kind": "one_person_two_ids", "patient_ids": ["a", "b"]},
        {"kind": "one_person_two_ids_variant", "patient_ids": ["c", "d"]},
    ]
    flags = {"conflict_ids": {"p1", "zz"}, "exact_pairs": {("a", "b")}, "fuzzy_pairs": {("c", "d"), ("x", "y")}}
    result = score_identity_checks.score(truth, flags)
    assert result["two_people_one_id"] == {"injected": 1, "flagged": 1, "false_flags": 1}
    assert result["one_person_two_ids_variant"]["caught_by_fuzzy_check"] == 1
    assert result["one_person_two_ids_variant"]["caught_by_exact_check"] == 0
    assert result["false_duplicate_pairs"] == {"exact_check": 0, "fuzzy_check": 1}


def test_local_replica_finds_every_injected_case_in_a_simulated_feed(tmp_path):
    random.seed(7)
    population = Population(
        target_size=60,
        rng=random.Random(7),
        identity_collision_rate=0.03,
        duplicate_person_rate=0.06,
        duplicate_variant_rate=0.5,
    )
    out = tmp_path / "patient_events"
    generate_files(
        out,
        patients=60,
        duration=300,
        seconds_per_file=60,
        end=datetime(2026, 10, 7, 2, 28, tzinfo=UTC),
        population=population,
    )
    result = score_identity_checks.score(
        json.loads(json.dumps(population.injected_identity)), score_identity_checks.flags_from_files(out)
    )
    assert result["two_people_one_id"]["flagged"] == result["two_people_one_id"]["injected"] > 0
    for kind in ("one_person_two_ids", "one_person_two_ids_variant"):
        assert result[kind]["caught_by_either"] == result[kind]["injected"]
