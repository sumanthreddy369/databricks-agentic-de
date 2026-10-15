"""Scores the patient-identity checks (problem catalog M2) against the
simulator's ground truth: what was injected vs what the Silver checks flagged.

    # flags from the live workspace (DATABRICKS_HOST, DATABRICKS_TOKEN, DATABRICKS_WAREHOUSE_ID)
    uv run python scripts/score_identity_checks.py data/landing_v5/patient_events_injected_identity.json
    # or computed locally with DuckDB from the landed files (same logic as Silver)
    uv run python scripts/score_identity_checks.py <manifest> --local data/landing_v5/patient_events

Writes docs/comparisons/results/identity_checks_<source>.json.
"""

import argparse
import itertools
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from agent import identity_review  # noqa: E402

OUT_DIR = REPO / "docs" / "comparisons" / "results"


def flags_of(candidates: list[dict]) -> dict:
    by_check = {}
    for c in candidates:
        by_check.setdefault(c["check"], set()).add(tuple(c["patient_ids"]))
    return {
        "conflict_ids": {ids[0] for ids in by_check.get("two_people_one_id", set())},
        "exact_pairs": by_check.get("exact_duplicate", set()),
        "fuzzy_pairs": by_check.get("fuzzy_duplicate", set()),
        "name_change_pairs": by_check.get("name_change", set()),
    }


def flags_from_workspace() -> dict:
    return flags_of(identity_review.candidates_from_workspace())


def flags_from_files(events_dir: Path) -> dict:
    """The Silver checks, re-expressed in DuckDB SQL over the raw files."""
    return flags_of(identity_review.candidates_from_files(events_dir))


def _same_person_pairs(pairs: set[tuple[str, str]]) -> set[tuple[str, str]]:
    """Every pair of ids planted for the same person. A person duplicated twice
    is planted as (original, copy1) and (original, copy2); (copy1, copy2) is
    the same person too, so flagging it isn't a false alarm."""
    group = {}
    for a, b in pairs:
        merged = group.get(a, {a}) | group.get(b, {b})
        for pid in merged:
            group[pid] = merged
    return {tuple(sorted(p)) for ids in group.values() for p in itertools.combinations(sorted(ids), 2)}


def score(truth: list[dict], flags: dict) -> dict:
    collisions = {t["patient_ids"][0] for t in truth if t["kind"] == "two_people_one_id"}
    by_kind = {
        kind: {tuple(t["patient_ids"]) for t in truth if t["kind"] == kind}
        for kind in ("one_person_two_ids", "one_person_two_ids_variant", "one_person_two_ids_renamed")
    }
    all_truth_pairs = _same_person_pairs(set().union(*by_kind.values()))
    checks = {"exact": "exact_pairs", "fuzzy": "fuzzy_pairs", "name_change": "name_change_pairs"}
    pair_flags = {name: flags.get(field, set()) for name, field in checks.items()}
    flagged = set().union(*pair_flags.values())
    result = {
        "two_people_one_id": {
            "injected": len(collisions),
            "flagged": len(collisions & flags["conflict_ids"]),
            "false_flags": len(flags["conflict_ids"] - collisions),
        }
    }
    for kind, pairs in by_kind.items():
        if not pairs and kind == "one_person_two_ids_renamed":
            continue  # feeds generated before renames existed
        result[kind] = {
            "injected": len(pairs),
            **{f"caught_by_{name}_check": len(pairs & found) for name, found in pair_flags.items()},
            "caught_by_any": len(pairs & flagged),
        }
    # Pairs a check raised that aren't the same planted person: two different
    # people can share a name and birth date by chance - why a steward decides.
    result["unplanted_pairs_flagged"] = {
        f"{name}_check": len(found - all_truth_pairs) for name, found in pair_flags.items()
    }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("manifest", help="ground truth written by simulator.file_feed")
    parser.add_argument("--local", default=None, help="compute the flags from these landed files instead")
    parser.add_argument("--out", default=None, help="write the report here instead of docs/comparisons/results/")
    parser.add_argument(
        "--require-all", action="store_true", help="exit 1 unless every planted case was caught (CI)"
    )
    args = parser.parse_args(argv)
    truth = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    flags = flags_from_files(Path(args.local)) if args.local else flags_from_workspace()
    report = {"source": "local DuckDB replica" if args.local else "Databricks workspace", **score(truth, flags)}
    out = (
        Path(args.out) if args.out else OUT_DIR / f"identity_checks_{'local' if args.local else 'workspace'}.json"
    )
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=1))
    print(f"wrote {out}")
    missed = missed_cases(report)
    if args.require_all and missed:
        print(f"MISSED planted cases: {missed}")
        return 1
    return 0


def missed_cases(report: dict) -> dict:
    """Planted cases a check didn't catch, by kind (empty means all caught)."""
    missed = {}
    two = report.get("two_people_one_id", {})
    if two.get("flagged", 0) < two.get("injected", 0):
        missed["two_people_one_id"] = two["injected"] - two["flagged"]
    for kind, r in report.items():
        if isinstance(r, dict) and "caught_by_any" in r and r["caught_by_any"] < r["injected"]:
            missed[kind] = r["injected"] - r["caught_by_any"]
    return missed


if __name__ == "__main__":
    sys.exit(main())
