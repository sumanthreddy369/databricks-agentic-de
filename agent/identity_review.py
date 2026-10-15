"""Data-steward review queue for the patient-identity checks (problem
catalog M2). The Silver checks only flag; a person decides, here:

    uv run python -m agent.identity_review summary
    uv run python -m agent.identity_review list
    uv run python -m agent.identity_review decide pt_0fbbc1b9,pt_c9acfc8e --same-person --by "Jane Doe"
    uv run python -m agent.identity_review decide pt_d919a719 --different-people --by "Jane Doe" --note "twins"
    uv run python -m agent.identity_review publish

Candidates come from the workspace's Silver tables (DATABRICKS_HOST,
DATABRICKS_TOKEN, DATABRICKS_WAREHOUSE_ID), or with `--local DIR` from a
DuckDB replica of the same logic over landed files. They carry patient_ids
and the reason only - never names or birth dates.

A decision is recorded in the state file (`identity_decisions`) and the
audit log, and the pair leaves the queue. Nothing is merged or split here:
"same person" is the input a master-patient-index merge needs; "different
people" stops the pair being raised again. `publish` copies the decisions to
healthcare_agentic_de.ops.identity_decisions, which the Gold table
dim_patient_person reads to give confirmed ids one person_key. Like agent/approvals.py, this
never calls a model and isn't exposed to the model as a tool; `--by` is a
recorded name, not an authenticated identity.
"""

import argparse
import itertools
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from agent.healthcheck import DEFAULT_STATE_PATH
from agent.orchestrator import DEFAULT_AUDIT_LOG_PATH
from agent.tools import pipeline_health
from common.identity import FORMAL_FIRST_NAME

CATALOG = "healthcare_agentic_de"

# check -> what a "yes" on it means
CHECKS = {
    "two_people_one_id": "different people admitted under one patient_id",
    "exact_duplicate": "one person under several patient_ids (same name and birth date)",
    "fuzzy_duplicate": "one person under two patient_ids (name typo or day/month swapped)",
    "name_change": "one person under two patient_ids (nickname or new surname)",
}
DECISIONS = ("same_person", "different_people")


def key_of(patient_ids) -> str:
    return ",".join(sorted(patient_ids))


def _pairs(ids) -> list[list[str]]:
    return [list(p) for p in itertools.combinations(sorted(set(ids)), 2)]


def candidates_from_workspace(client=None) -> list[dict]:
    if client is None:
        from agent.databricks_client import DatabricksClient, load_config

        client = DatabricksClient(load_config(), timeout=120)
    silver = f"{CATALOG}.silver"
    out = [
        {"check": "two_people_one_id", "patient_ids": [r["patient_id"]], "detail": f"{r['admissions']} admissions"}
        for r in client.execute_statement(
            f"SELECT patient_id, admissions FROM {silver}.silver_patient_identity_conflicts LIMIT 5000"
        )
    ]
    for r in client.execute_statement(
        f"SELECT to_json(candidate_patient_ids) AS ids "
        f"FROM {silver}.silver_patient_duplicate_candidates LIMIT 5000"
    ):
        out += [
            {"check": "exact_duplicate", "patient_ids": p, "detail": "same name and birth date"}
            for p in _pairs(json.loads(r["ids"]))
        ]
    for r in client.execute_statement(
        f"SELECT patient_id_a, patient_id_b, name_edit_distance, birth_date_match "
        f"FROM {silver}.silver_patient_fuzzy_duplicate_candidates LIMIT 5000"
    ):
        out.append(
            {
                "check": "fuzzy_duplicate",
                "patient_ids": sorted([r["patient_id_a"], r["patient_id_b"]]),
                "detail": f"name {r['name_edit_distance']} edit(s) apart; birth date {r['birth_date_match']}",
            }
        )
    for r in client.execute_statement(
        f"SELECT patient_id_a, patient_id_b, match_reason FROM {silver}.silver_patient_name_change_candidates "
        "LIMIT 5000"
    ):
        out.append(
            {
                "check": "name_change",
                "patient_ids": sorted([r["patient_id_a"], r["patient_id_b"]]),
                "detail": r["match_reason"],
            }
        )
    return out


def candidates_from_files(events_dir: Path) -> list[dict]:
    """The four Silver identity checks, re-expressed in DuckDB SQL over the
    landed files (pipeline/03_silver/silver_encounters.py is the original)."""
    import duckdb

    src = f"read_json_auto('{Path(events_dir).as_posix()}/*.jsonl', union_by_name=true)"
    con = duckdb.connect()
    con.execute(
        f"""CREATE VIEW adm AS SELECT patient_id, encounter_id, patient.full_name AS full_name,
            CAST(patient.birth_date AS VARCHAR) AS birth_date, patient.gender AS gender FROM {src}
            WHERE event_type = 'admitted' AND patient IS NOT NULL"""
    )
    con.execute("CREATE TABLE nicknames (first_name VARCHAR, formal_first_name VARCHAR)")
    con.executemany("INSERT INTO nicknames VALUES (?, ?)", sorted(FORMAL_FIRST_NAME.items()))
    out = [
        {"check": "two_people_one_id", "patient_ids": [pid], "detail": f"{n} admissions"}
        for pid, n in con.sql(
            """SELECT patient_id, count(DISTINCT encounter_id) FROM adm GROUP BY 1
               HAVING count(DISTINCT encounter_id) > 1
                  AND (count(DISTINCT full_name) > 1 OR count(DISTINCT birth_date) > 1)"""
        ).fetchall()
    ]
    for (ids,) in con.sql(
        "SELECT list(DISTINCT patient_id) FROM adm GROUP BY full_name, birth_date "
        "HAVING count(DISTINCT patient_id) > 1"
    ).fetchall():
        out += [
            {"check": "exact_duplicate", "patient_ids": p, "detail": "same name and birth date"}
            for p in _pairs(ids)
        ]
    for a, b, d, match in con.sql(
        """WITH p AS (SELECT DISTINCT patient_id, lower(full_name) AS name, birth_date, gender,
                 gender || '|' || substr(birth_date, 1, 4) || '|' ||
                 least(substr(birth_date, 6, 2), substr(birth_date, 9, 2)) || '-' ||
                 greatest(substr(birth_date, 6, 2), substr(birth_date, 9, 2)) AS block FROM adm)
           SELECT a.patient_id, b.patient_id, levenshtein(a.name, b.name),
                  CASE WHEN a.birth_date = b.birth_date THEN 'same' ELSE 'day and month swapped' END
           FROM p a JOIN p b ON a.block = b.block AND a.patient_id < b.patient_id
           WHERE levenshtein(a.name, b.name) <= 2
             AND NOT (levenshtein(a.name, b.name) = 0 AND a.birth_date = b.birth_date)"""
    ).fetchall():
        out.append(
            {
                "check": "fuzzy_duplicate",
                "patient_ids": sorted([a, b]),
                "detail": f"name {d} edit(s) apart; birth date {match}",
            }
        )
    for a, b, reason in con.sql(
        r"""WITH n AS (SELECT DISTINCT patient_id, lower(full_name) AS name, birth_date, gender FROM adm),
                s AS (SELECT *, regexp_extract(name, '^(\S+)', 1) AS first_name,
                             regexp_extract(name, '^\S+\s+(.*)$', 1) AS last_name FROM n),
                p AS (SELECT s.*, gender || '|' || birth_date || '|' || coalesce(k.formal_first_name, s.first_name)
                             AS block FROM s LEFT JOIN nicknames k USING (first_name))
           SELECT a.patient_id, b.patient_id,
                  CASE WHEN a.last_name = b.last_name THEN 'nickname'
                       WHEN a.first_name = b.first_name THEN 'surname changed'
                       ELSE 'nickname and surname changed' END
           FROM p a JOIN p b ON a.block = b.block AND a.patient_id < b.patient_id
           WHERE levenshtein(a.name, b.name) > 2"""
    ).fetchall():
        out.append({"check": "name_change", "patient_ids": sorted([a, b]), "detail": reason})
    return out


def queue(candidates: list[dict], state: dict) -> list[dict]:
    """Candidates no one has decided yet."""
    decided = state.get("identity_decisions", {})
    return [c for c in candidates if key_of(c["patient_ids"]) not in decided]


def decide(state: dict, patient_ids, decision: str, by: str, note: str = "", *, now=None) -> dict:
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}")
    if not by.strip():
        raise ValueError("a decision needs the name of the person making it (--by)")
    ids = sorted(patient_ids)
    if not 1 <= len(ids) <= 2 or not all(i.startswith("pt_") for i in ids):
        raise ValueError("give one patient_id (shared id) or two (a duplicate pair)")
    record = {
        "decision": decision,
        "by": by,
        "at": (now or datetime.now(UTC)).isoformat(),
        "note": note,
    }
    state.setdefault("identity_decisions", {})[key_of(ids)] = record
    return {"patient_ids": ids, **record}


def summary(candidates: list[dict], state: dict) -> dict:
    decided = state.get("identity_decisions", {})
    out = {}
    for check in CHECKS:
        keys = {key_of(c["patient_ids"]) for c in candidates if c["check"] == check}
        made = [decided[k]["decision"] for k in keys if k in decided]
        out[check] = {
            "candidates": len(keys),
            "pending": len(keys) - len(made),
            **{d: made.count(d) for d in DECISIONS},
        }
    return out


DECISIONS_TABLE = f"{CATALOG}.ops.identity_decisions"
_DECISION_TYPE = (
    "array<struct<patient_ids:string,decision:string,decided_by:string,decided_at:string,note:string>>"
)


def publish(state: dict, client=None) -> int:
    """Replace ops.identity_decisions with the state file's decisions (the
    state file is the record; the table is a copy for the pipeline). One
    bound JSON parameter - decision text never becomes SQL."""
    if client is None:
        from agent.databricks_client import DatabricksClient, load_config

        client = DatabricksClient(load_config(), timeout=120)
    rows = [
        {
            "patient_ids": key,
            "decision": d["decision"],
            "decided_by": d["by"],
            "decided_at": d["at"],
            "note": d["note"],
        }
        for key, d in sorted(state.get("identity_decisions", {}).items())
    ]
    client.execute_statement(
        f"CREATE OR REPLACE TABLE {DECISIONS_TABLE} "
        "COMMENT 'Data-steward decisions on patient-identity candidates (agent/identity_review.py)' AS "
        "SELECT d.patient_ids, d.decision, d.decided_by, CAST(d.decided_at AS TIMESTAMP) AS decided_at, d.note "
        f"FROM (SELECT explode(from_json(:decisions, '{_DECISION_TYPE}')) AS d)",
        [{"name": "decisions", "value": json.dumps(rows), "type": "STRING"}],
    )
    return len(rows)


def _append_audit(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, default=str) + "\n"
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:  # Unity Catalog volumes don't support append; see orchestrator._append_audit_log
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        path.write_text(existing + line, encoding="utf-8")


def run(argv: list[str] | None = None, *, client=None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent.identity_review", description="Review patient-identity candidates."
    )
    parser.add_argument("--state-path", default=os.environ.get("PIPELINE_STATE_PATH", DEFAULT_STATE_PATH))
    parser.add_argument("--audit-log-path", default=os.environ.get("AUDIT_LOG_PATH", DEFAULT_AUDIT_LOG_PATH))
    parser.add_argument("--local", default=None, help="compute candidates from these landed files instead")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("summary", help="candidates, pending and decided, per check")
    sub.add_parser("list", help="candidates waiting for a decision")
    sub.add_parser("publish", help="copy the decisions to the workspace table the Gold person map reads")
    dec = sub.add_parser("decide", help="record a decision on one candidate")
    dec.add_argument("patient_ids", help="one patient_id, or two separated by a comma")
    choice = dec.add_mutually_exclusive_group(required=True)
    choice.add_argument("--same-person", dest="decision", action="store_const", const="same_person")
    choice.add_argument("--different-people", dest="decision", action="store_const", const="different_people")
    dec.add_argument("--by", required=True, help="name of the person deciding")
    dec.add_argument("--note", default="")
    args = parser.parse_args(argv)

    state_path = Path(args.state_path)
    # No state file yet just means no decisions yet.
    state = pipeline_health.load_state(state_path) if state_path.exists() else {}
    if args.command == "decide":
        try:
            record = decide(state, args.patient_ids.split(","), args.decision, args.by, args.note)
        except ValueError as exc:
            print(json.dumps({"error": str(exc)}))
            return 1
        state_path.parent.mkdir(parents=True, exist_ok=True)
        pipeline_health.save_state(state_path, state)
        _append_audit(
            Path(args.audit_log_path),
            {
                "ts": record["at"],
                "mode": "steward",
                "tool": "identity_decision",
                "input": record,
                "is_error": False,
            },
        )
        print(json.dumps(record))
        return 0

    if args.command == "publish":
        print(json.dumps({"published": publish(state, client), "table": DECISIONS_TABLE}))
        return 0

    candidates = candidates_from_files(Path(args.local)) if args.local else candidates_from_workspace(client)
    if args.command == "summary":
        print(json.dumps(summary(candidates, state), indent=2))
    else:
        print(json.dumps(queue(candidates, state), indent=2))
    return 0


def main() -> None:
    sys.exit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
