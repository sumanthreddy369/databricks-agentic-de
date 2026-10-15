"""Records what the pipeline produced on a live workspace, as evidence in the repo.

    uv run python scripts/workspace_snapshot.py <streaming_pipeline_id> [--label free_edition]

Needs DATABRICKS_HOST, DATABRICKS_TOKEN (e.g. `databricks auth token`) and
DATABRICKS_WAREHOUSE_ID. Writes docs/comparisons/results/workspace_<label>_<date>.json:

- row counts of every table the pipelines published (bronze/silver/gold);
- every expectation's passed / failed records, from the pipeline's event log;
- the Unity Catalog mask check (names and MRNs redacted in Gold);
- the patient identity checks (problem catalog M2): two people on one id, one person on two ids.

Counts and flags only - never patient values.
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from agent.databricks_client import DatabricksClient, load_config  # noqa: E402

CATALOG = "healthcare_agentic_de"
OUT_DIR = REPO / "docs" / "comparisons" / "results"


def snapshot(client: DatabricksClient, pipeline_id: str) -> dict:
    tables = client.execute_statement(
        f"SELECT table_schema, table_name, table_type FROM {CATALOG}.information_schema.tables "
        "WHERE table_schema IN ('bronze', 'silver', 'gold') AND table_name NOT LIKE '\\_\\_%' "
        "ORDER BY table_schema, table_name LIMIT 200"
    )
    counts = []
    for t in tables:
        name = f"{CATALOG}.{t['table_schema']}.{t['table_name']}"
        n = client.execute_statement(f"SELECT count(*) AS n FROM {name} LIMIT 1")[0]["n"]
        counts.append({"table": f"{t['table_schema']}.{t['table_name']}", "type": t["table_type"], "rows": int(n)})

    expectations = client.execute_statement(
        "SELECT e.dataset AS dataset, e.name AS expectation, sum(e.passed_records) AS passed, "
        "sum(e.failed_records) AS failed FROM (SELECT explode(from_json("
        "details:flow_progress.data_quality.expectations, "
        "'array<struct<name:string,dataset:string,passed_records:bigint,failed_records:bigint>>')) AS e "
        f"FROM event_log('{pipeline_id}') WHERE event_type = 'flow_progress' "
        # Only the latest update: the event log keeps every run, and summing them double-counts.
        f"AND origin.update_id = (SELECT origin.update_id FROM event_log('{pipeline_id}') "
        "WHERE event_type = 'create_update' ORDER BY timestamp DESC LIMIT 1)) "
        "GROUP BY 1, 2 ORDER BY 1, 2 LIMIT 200"
    )
    masks = client.execute_statement(
        f"SELECT count(*) AS patients, count_if(full_name = '***REDACTED***') AS names_masked, "
        f"count_if(mrn = '***REDACTED***') AS mrns_masked FROM {CATALOG}.gold.dim_patients LIMIT 1"
    )[0]
    identity = client.execute_statement(
        f"SELECT count(*) AS patient_ids_admitted_more_than_once, "
        f"count_if(names > 1 OR birth_dates > 1) AS flagged_two_people_one_id "
        f"FROM {CATALOG}.silver.silver_patient_identity_conflicts LIMIT 1"
    )[0]
    duplicates = client.execute_statement(
        f"SELECT count(*) AS candidate_groups, coalesce(sum(patient_ids), 0) AS patient_ids_involved "
        f"FROM {CATALOG}.silver.silver_patient_duplicate_candidates LIMIT 1"
    )[0]
    identity["duplicate_person_candidate_groups"] = duplicates["candidate_groups"]
    identity["duplicate_person_patient_ids_involved"] = duplicates["patient_ids_involved"]
    fuzzy = client.execute_statement(
        f"SELECT count(*) AS pairs FROM {CATALOG}.silver.silver_patient_fuzzy_duplicate_candidates LIMIT 1"
    )[0]
    identity["near_duplicate_pairs"] = fuzzy["pairs"]
    renamed = client.execute_statement(
        f"SELECT count(*) AS pairs FROM {CATALOG}.silver.silver_patient_name_change_candidates LIMIT 1"
    )[0]
    identity["name_change_pairs"] = renamed["pairs"]
    return {
        "tables": counts,
        "expectations": [
            {
                "dataset": e["dataset"].split(".")[-1],
                "expectation": e["expectation"],
                "passed": int(e["passed"]),
                "failed": int(e["failed"]),
            }
            for e in expectations
        ],
        "masks": {k: int(v) for k, v in masks.items()},
        "identity_check": {k: int(v) for k, v in identity.items()},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("pipeline_id", help="streaming_patient_pipeline id (for its event log)")
    parser.add_argument("--label", default="free_edition")
    parser.add_argument("--note", default="", help="what data / run this describes")
    args = parser.parse_args(argv)
    config = load_config()
    if config is None or not config.warehouse_id:
        print("needs DATABRICKS_HOST, DATABRICKS_TOKEN, DATABRICKS_WAREHOUSE_ID", file=sys.stderr)
        return 2
    client = DatabricksClient(config, timeout=120)
    body = {
        "measured_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "workspace": args.label,
        "note": args.note,
        **snapshot(client, args.pipeline_id),
    }
    out = OUT_DIR / f"workspace_{args.label}_{datetime.now(UTC):%Y%m%d}.json"
    out.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(body, indent=1))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
