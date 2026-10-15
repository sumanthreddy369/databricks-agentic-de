"""Sets up a Databricks workspace for this project from the repo, in one
idempotent command - what was done by hand on Free Edition:

    uv run python scripts/setup_workspace.py --profile free --target free --dry-run
    uv run python scripts/setup_workspace.py --profile free --target free \
        --patient-events data/landing_v5/patient_events --feed-name patient_events_v5 --run-pipeline

Steps, each safe to repeat:
1. Unity Catalog objects: governance/05_unity_catalog/catalog_and_grants.sql
   then row_filters_and_masking.sql, one statement at a time on the SQL
   warehouse. Group creation and grants are account-security changes, so
   they're left out (listed in the report) unless `--with-groups` is given -
   then they're tried and any that fail (no such group) are reported;
   anything else that fails stops the setup.
2. Landing files: the provider roster and a patient-event feed uploaded to
   the bronze.landing volume, unless that folder already has files.
3. Secret scope `healthcare_agentic_de` (empty; secrets are put by a person).
4. `databricks bundle deploy`, then optionally a pipeline run.

Not covered: the Genie space (create it in the UI, then
scripts/sync_genie_space.py <space_id> writes its instructions from the repo).
Uses the Databricks CLI for everything; credentials stay with the CLI profile.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SQL_FILES = [
    REPO / "governance" / "05_unity_catalog" / "catalog_and_grants.sql",
    REPO / "governance" / "05_unity_catalog" / "row_filters_and_masking.sql",
]
VOLUME = "/Volumes/healthcare_agentic_de/bronze/landing"
PROVIDERS = REPO / "data" / "landing" / "providers"  # simulator.autoloader_feed output
SECRET_SCOPE = "healthcare_agentic_de"


def statements(sql: str) -> list[str]:
    """SQL file -> statements: `--` comments dropped, split on `;` at a line end."""
    lines = [line for line in sql.splitlines() if not line.lstrip().startswith("--")]
    return [s.strip() for s in re.split(r";\s*$", "\n".join(lines), flags=re.M) if s.strip()]


def needs_a_person(statement: str) -> bool:
    """Group creation and grants: account-security changes."""
    s = statement.lstrip().upper()
    return s.startswith("CREATE GROUP") or s.startswith("GRANT ")


def _cli(args: list[str], profile: str, *, check: bool = True) -> subprocess.CompletedProcess:
    # A list, never a shell string: SQL contains `>` and quotes.
    return subprocess.run(
        ["databricks", *args, "--profile", profile], capture_output=True, text=True, check=check, cwd=REPO
    )


def run_sql(statement: str, profile: str, warehouse_id: str) -> tuple[bool, str]:
    body = {"warehouse_id": warehouse_id, "statement": statement, "wait_timeout": "50s"}
    out = _cli(["api", "post", "/api/2.0/sql/statements/", "--json", json.dumps(body)], profile, check=False)
    if out.returncode != 0:
        return False, (out.stderr or out.stdout).strip()[:300]
    status = json.loads(out.stdout).get("status", {})
    if status.get("state") == "SUCCEEDED":
        return True, ""
    return False, status.get("error", {}).get("message", status.get("state", "unknown"))[:300]


def warehouse(profile: str) -> str:
    rows = json.loads(_cli(["warehouses", "list", "-o", "json"], profile).stdout)
    if not rows:
        raise SystemExit("no SQL warehouse in this workspace")
    return rows[0]["id"]


def folder_has_files(path: str, profile: str) -> bool:
    out = _cli(["fs", "ls", f"dbfs:{path}"], profile, check=False)
    return out.returncode == 0 and bool(out.stdout.strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--profile", required=True, help="Databricks CLI profile")
    parser.add_argument("--target", required=True, help="bundle target in databricks.yml")
    parser.add_argument(
        "--patient-events", type=Path, help="local feed folder to upload (simulator.file_feed output)"
    )
    parser.add_argument("--feed-name", default="patient_events", help="folder name under the landing volume")
    parser.add_argument("--run-pipeline", action="store_true")
    parser.add_argument("--with-groups", action="store_true", help="also run group creation and grants")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    args = parser.parse_args(argv)

    plan = [s for f in SQL_FILES for s in statements(f.read_text(encoding="utf-8"))]
    if args.dry_run:
        for s in plan:
            label = (
                ("TRY  " if args.with_groups else "LEAVE (needs --with-groups) ") if needs_a_person(s) else "SQL  "
            )
            print(label + " ".join(s.split())[:100])
        print(f"UPLOAD {PROVIDERS.relative_to(REPO)} -> {VOLUME}/providers (if empty)")
        if args.patient_events:
            print(f"UPLOAD {args.patient_events} -> {VOLUME}/{args.feed_name} (if empty)")
        print(f"SECRET SCOPE {SECRET_SCOPE} (if missing)")
        print(f"BUNDLE DEPLOY -t {args.target}" + (" + pipeline run" if args.run_pipeline else ""))
        return 0

    wh = warehouse(args.profile)
    report = {"ran": 0, "left_for_a_person": 0, "skipped": []}
    for s in plan:
        if needs_a_person(s) and not args.with_groups:
            report["left_for_a_person"] += 1
            continue
        ok, error = run_sql(s, args.profile, wh)
        if ok:
            report["ran"] += 1
        elif needs_a_person(s):
            report["skipped"].append({"statement": " ".join(s.split())[:90], "why": error[:120]})
        else:
            print(json.dumps(report, indent=1))
            raise SystemExit(f"failed: {' '.join(s.split())[:120]}\n{error}")

    uploads = [(PROVIDERS, "providers")]
    if args.patient_events:
        uploads.append((args.patient_events, args.feed_name))
    report["uploaded"] = []
    for local, name in uploads:
        target = f"{VOLUME}/{name}"
        if folder_has_files(target, args.profile):
            continue
        _cli(["fs", "cp", "-r", str(local), f"dbfs:{target}"], args.profile)
        report["uploaded"].append(target)

    scopes = json.loads(_cli(["secrets", "list-scopes", "-o", "json"], args.profile).stdout or "[]")
    if SECRET_SCOPE not in {s["name"] for s in scopes}:
        _cli(["secrets", "create-scope", SECRET_SCOPE], args.profile)
        report["secret_scope"] = "created"
    else:
        report["secret_scope"] = "exists"

    _cli(["bundle", "deploy", "-t", args.target], args.profile)
    report["bundle"] = f"deployed -t {args.target}"
    if args.run_pipeline:
        _cli(["bundle", "run", "streaming_patient_pipeline", "-t", args.target], args.profile)
        report["pipeline"] = "update completed"
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
