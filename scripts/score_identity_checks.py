"""Scores the patient-identity checks (problem catalog M2) against the
simulator's ground truth: what was injected vs what the Silver checks flagged.

    # flags from the live workspace (DATABRICKS_HOST, DATABRICKS_TOKEN, DATABRICKS_WAREHOUSE_ID)
    uv run python scripts/score_identity_checks.py data/landing_v4/patient_events_injected_identity.json
    # or computed locally with DuckDB from the landed files (same logic as Silver)
    uv run python scripts/score_identity_checks.py <manifest> --local data/landing_v4/patient_events

Writes docs/comparisons/results/identity_checks_<source>.json.
"""

import argparse
import itertools
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

OUT_DIR = REPO / "docs" / "comparisons" / "results"
CATALOG = "healthcare_agentic_de"


def _pairs(ids) -> set[tuple[str, str]]:
    return {tuple(sorted(p)) for p in itertools.combinations(sorted(set(ids)), 2)}


def flags_from_workspace() -> dict:
    from agent.databricks_client import DatabricksClient, load_config

    client = DatabricksClient(load_config(), timeout=120)
    silver = f"{CATALOG}.silver"
    conflicts = client.execute_statement(
        f"SELECT patient_id FROM {silver}.silver_patient_identity_conflicts LIMIT 5000"
    )
    exact = client.execute_statement(
        f"SELECT to_json(candidate_patient_ids) AS ids FROM {silver}.silver_patient_duplicate_candidates LIMIT 5000"
    )
    fuzzy = client.execute_statement(
        f"SELECT patient_id_a, patient_id_b, birth_date_match FROM {silver}.silver_patient_fuzzy_duplicate_candidates "
        "LIMIT 5000"
    )
    return {
        "conflict_ids": {r["patient_id"] for r in conflicts},
        "exact_pairs": set().union(*[_pairs(json.loads(r["ids"])) for r in exact]) if exact else set(),
        "fuzzy_pairs": {tuple(sorted((r["patient_id_a"], r["patient_id_b"]))) for r in fuzzy},
    }


def flags_from_files(events_dir: Path) -> dict:
    """The three Silver checks, re-expressed in DuckDB SQL over the raw files."""
    import duckdb

    src = f"read_json_auto('{events_dir.as_posix()}/*.jsonl', union_by_name=true)"
    con = duckdb.connect()
    con.execute(
        f"""CREATE VIEW adm AS SELECT patient_id, encounter_id, patient.full_name AS full_name,
            CAST(patient.birth_date AS VARCHAR) AS birth_date, patient.gender AS gender FROM {src}
            WHERE event_type = 'admitted' AND patient IS NOT NULL"""
    )
    conflicts = con.sql(
        """SELECT patient_id FROM adm GROUP BY 1
           HAVING count(DISTINCT encounter_id) > 1 AND (count(DISTINCT full_name) > 1 OR count(DISTINCT birth_date) > 1)"""
    ).fetchall()
    exact = con.sql(
        "SELECT list(DISTINCT patient_id) FROM adm GROUP BY full_name, birth_date HAVING count(DISTINCT patient_id) > 1"
    ).fetchall()
    fuzzy = con.sql(
        """WITH p AS (SELECT DISTINCT patient_id, lower(full_name) AS name, birth_date, gender,
                 gender || '|' || substr(birth_date, 1, 4) || '|' ||
                 least(substr(birth_date, 6, 2), substr(birth_date, 9, 2)) || '-' ||
                 greatest(substr(birth_date, 6, 2), substr(birth_date, 9, 2)) AS block FROM adm)
           SELECT a.patient_id, b.patient_id FROM p a JOIN p b ON a.block = b.block AND a.patient_id < b.patient_id
           WHERE levenshtein(a.name, b.name) <= 2
             AND NOT (levenshtein(a.name, b.name) = 0 AND a.birth_date = b.birth_date)"""
    ).fetchall()
    return {
        "conflict_ids": {r[0] for r in conflicts},
        "exact_pairs": set().union(*[_pairs(r[0]) for r in exact]) if exact else set(),
        "fuzzy_pairs": {tuple(sorted(r)) for r in fuzzy},
    }


def score(truth: list[dict], flags: dict) -> dict:
    collisions = {t["patient_ids"][0] for t in truth if t["kind"] == "two_people_one_id"}
    by_kind = {
        kind: {tuple(t["patient_ids"]) for t in truth if t["kind"] == kind}
        for kind in ("one_person_two_ids", "one_person_two_ids_variant")
    }
    all_truth_pairs = set().union(*by_kind.values())
    flagged = flags["exact_pairs"] | flags["fuzzy_pairs"]
    result = {
        "two_people_one_id": {
            "injected": len(collisions),
            "flagged": len(collisions & flags["conflict_ids"]),
            "false_flags": len(flags["conflict_ids"] - collisions),
        }
    }
    for kind, pairs in by_kind.items():
        result[kind] = {
            "injected": len(pairs),
            "caught_by_exact_check": len(pairs & flags["exact_pairs"]),
            "caught_by_fuzzy_check": len(pairs & flags["fuzzy_pairs"]),
            "caught_by_either": len(pairs & flagged),
        }
    result["false_duplicate_pairs"] = {
        "exact_check": len(flags["exact_pairs"] - all_truth_pairs),
        "fuzzy_check": len(flags["fuzzy_pairs"] - all_truth_pairs),
    }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("manifest", help="ground truth written by simulator.file_feed")
    parser.add_argument("--local", default=None, help="compute the flags from these landed files instead")
    args = parser.parse_args(argv)
    truth = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    flags = flags_from_files(Path(args.local)) if args.local else flags_from_workspace()
    report = {"source": "local DuckDB replica" if args.local else "Databricks workspace", **score(truth, flags)}
    out = OUT_DIR / f"identity_checks_{'local' if args.local else 'workspace'}.json"
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=1))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
