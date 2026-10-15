"""The data-steward review queue (agent/identity_review.py): candidates from
the identity checks wait until a named person decides, and a decision is
recorded in the state file and the audit log - never a merge."""

import json
import random
from datetime import UTC, datetime

import pytest

from agent import identity_review
from simulator.file_feed import generate_files
from simulator.population import Population

CANDIDATES = [
    {"check": "two_people_one_id", "patient_ids": ["pt_00000001"], "detail": "2 admissions"},
    {"check": "fuzzy_duplicate", "patient_ids": ["pt_0000000a", "pt_0000000b"], "detail": "name 1 edit(s) apart"},
    {"check": "name_change", "patient_ids": ["pt_0000000c", "pt_0000000d"], "detail": "nickname"},
]


def test_a_decision_takes_the_pair_off_the_queue():
    state = {}
    identity_review.decide(state, ["pt_0000000b", "pt_0000000a"], "same_person", "Jane Doe")
    assert [c["check"] for c in identity_review.queue(CANDIDATES, state)] == ["two_people_one_id", "name_change"]
    summary = identity_review.summary(CANDIDATES, state)
    assert summary["fuzzy_duplicate"] == {"candidates": 1, "pending": 0, "same_person": 1, "different_people": 0}
    assert summary["name_change"]["pending"] == 1


@pytest.mark.parametrize(
    "ids, decision, by",
    [
        (["pt_1", "pt_2"], "merge", "Jane"),  # not a decision a steward can make here
        (["pt_1", "pt_2"], "same_person", " "),  # nobody named
        (["pt_1", "pt_2", "pt_3"], "same_person", "Jane"),
        (["Mary Johnson"], "same_person", "Jane"),  # ids only, never names
    ],
)
def test_bad_decisions_are_refused(ids, decision, by):
    with pytest.raises(ValueError):
        identity_review.decide({}, ids, decision, by)


def test_cli_records_the_decision_in_state_and_audit_log(tmp_path):
    state_path, audit = tmp_path / "state.json", tmp_path / "audit.jsonl"
    code = identity_review.run(
        [
            "--state-path",
            str(state_path),
            "--audit-log-path",
            str(audit),
            "decide",
            "pt_0000000c,pt_0000000d",
            "--different-people",
            "--by",
            "Jane Doe",
            "--note",
            "siblings",
        ]
    )
    assert code == 0
    state = json.loads(state_path.read_text())
    assert state["identity_decisions"]["pt_0000000c,pt_0000000d"]["decision"] == "different_people"
    line = json.loads(audit.read_text().splitlines()[-1])
    assert line["tool"] == "identity_decision" and line["input"]["by"] == "Jane Doe"


def test_queue_from_landed_files_lists_ids_and_reasons_only(tmp_path, capsys):
    random.seed(11)
    population = Population(
        target_size=60, rng=random.Random(11), duplicate_person_rate=0.08, duplicate_rename_rate=1.0
    )
    out = tmp_path / "patient_events"
    generate_files(
        out,
        patients=60,
        duration=180,
        seconds_per_file=60,
        end=datetime(2026, 10, 7, 2, 28, tzinfo=UTC),
        population=population,
    )
    assert identity_review.run(["--state-path", str(tmp_path / "s.json"), "--local", str(out), "list"]) == 0
    queued = json.loads(capsys.readouterr().out)
    assert {c["check"] for c in queued} >= {"name_change"}
    text = json.dumps(queued)
    for snapshot in population.issued_people:
        assert snapshot.full_name not in text and snapshot.birth_date not in text


def test_person_keys_join_chains_of_confirmed_pairs():
    from common.identity import person_keys

    keys = person_keys([("pt_c", "pt_b"), ("pt_b", "pt_a"), ("pt_x", "pt_y")])
    assert keys == {"pt_a": "pt_a", "pt_b": "pt_a", "pt_c": "pt_a", "pt_x": "pt_x", "pt_y": "pt_x"}
    assert person_keys([]) == {}


class _FakeClient:
    def __init__(self):
        self.calls = []

    def execute_statement(self, statement, parameters=None):
        self.calls.append((statement, parameters))
        return []


def test_publish_binds_decisions_as_one_parameter():
    state = {}
    identity_review.decide(state, ["pt_0000000a", "pt_0000000b"], "same_person", "Robert'); DROP TABLE x; --")
    client = _FakeClient()
    assert identity_review.publish(state, client) == 1
    statement, params = client.calls[0]
    assert "DROP TABLE" not in statement and identity_review.DECISIONS_TABLE in statement
    rows = json.loads(params[0]["value"])
    assert rows[0]["patient_ids"] == "pt_0000000a,pt_0000000b" and rows[0]["decision"] == "same_person"


def test_simulated_steward_decides_from_ground_truth():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import simulate_steward

    truth = [
        {"kind": "one_person_two_ids", "patient_ids": ["pt_1", "pt_2"]},
        {"kind": "one_person_two_ids", "patient_ids": ["pt_1", "pt_3"]},
        {"kind": "two_people_one_id", "patient_ids": ["pt_9"]},
    ]
    candidates = [
        {"check": "exact_duplicate", "patient_ids": ["pt_2", "pt_3"], "detail": ""},  # same person via pt_1
        {"check": "exact_duplicate", "patient_ids": ["pt_4", "pt_5"], "detail": ""},  # chance match
        {"check": "two_people_one_id", "patient_ids": ["pt_9"], "detail": ""},
    ]
    state = {}
    assert simulate_steward.decide_all(candidates, truth, state) == {"same_person": 1, "different_people": 2}
    assert all(d["by"] == simulate_steward.STEWARD for d in state["identity_decisions"].values())
