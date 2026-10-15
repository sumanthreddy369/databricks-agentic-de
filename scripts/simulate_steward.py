"""Replays a data steward from the simulator's ground truth, to measure the
identity workflow end to end: every candidate in the review queue gets the
decision a perfect steward would make, recorded under a name that says so.
Real decisions are a person's (agent/identity_review.py decide).

    # candidates from the workspace, decisions into a state file, then published
    uv run python scripts/simulate_steward.py docs/comparisons/results/identity_ground_truth_v5.json \
        --state-path data/state/steward_v5.json --publish
    # or from the landed files
    uv run python scripts/simulate_steward.py <manifest> --local data/landing_v5/patient_events --state-path <file>
"""

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from agent import identity_review  # noqa: E402
from agent.tools import pipeline_health  # noqa: E402
from common.identity import person_keys  # noqa: E402

STEWARD = "simulated steward (ground-truth replay, scripts/simulate_steward.py)"


def same_person_groups(truth: list[dict]) -> dict[str, str]:
    """patient_id -> person key, for every id planted as a duplicate."""
    return person_keys(t["patient_ids"] for t in truth if len(t["patient_ids"]) == 2)


def decide_all(candidates: list[dict], truth: list[dict], state: dict) -> dict:
    person = same_person_groups(truth)
    shared = {t["patient_ids"][0] for t in truth if t["kind"] == "two_people_one_id"}
    counts = {"same_person": 0, "different_people": 0}
    for c in identity_review.queue(candidates, state):
        ids = c["patient_ids"]
        if len(ids) == 1:
            same = ids[0] not in shared  # a shared id is two people
        else:
            same = ids[0] in person and person.get(ids[0]) == person.get(ids[1])
        decision = "same_person" if same else "different_people"
        identity_review.decide(state, ids, decision, STEWARD, note=f"{c['check']}: {c['detail']}")
        counts[decision] += 1
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("manifest")
    parser.add_argument("--state-path", required=True)
    parser.add_argument(
        "--local", default=None, help="candidates from these landed files instead of the workspace"
    )
    parser.add_argument("--publish", action="store_true", help="copy the decisions to ops.identity_decisions")
    args = parser.parse_args(argv)
    truth = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    candidates = (
        identity_review.candidates_from_files(Path(args.local))
        if args.local
        else identity_review.candidates_from_workspace()
    )
    state_path = Path(args.state_path)
    state = pipeline_health.load_state(state_path) if state_path.exists() else {}
    counts = decide_all(candidates, truth, state)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    pipeline_health.save_state(state_path, state)
    report = {"decided": counts, "summary": identity_review.summary(candidates, state)}
    if args.publish:
        report["published"] = identity_review.publish(state)
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
