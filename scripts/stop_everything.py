"""Stop everything that can cost money in the Databricks workspace.

Run before the free trial ends (by hand or from a scheduled task):

    uv run python scripts/stop_everything.py --dry-run     # show what it would stop
    uv run python scripts/stop_everything.py               # stop it

What it does - all reversible, no data is deleted:
- jobs: cancels active runs and pauses every schedule;
- pipelines: stops any running update;
- SQL warehouses: stops any that are running;
- clusters: terminates any that are running (terminate, not delete).

`--destroy` additionally runs `databricks bundle destroy -t dev`, which removes
the jobs and pipelines this project deployed. Deleting a pipeline also drops
the tables it manages, so that one is not reversible; it never runs unless
asked for.

What it can't do: cancel the Databricks subscription or delete the workspace.
That's done by the account owner in GCP Marketplace / the Databricks account
console, and it's the only way to be sure nothing is billed after the trial.

Uses the Databricks CLI and its login (`databricks auth login`), so it needs
no token of its own. Exits non-zero if any stop failed, so a scheduled run
that didn't work is visible.
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNNING_CLUSTER_STATES = {"PENDING", "RUNNING", "RESIZING", "RESTARTING"}
IDLE_WAREHOUSE_STATES = {"STOPPED", "STOPPING", "DELETED", "DELETING"}
IDLE_PIPELINE_STATES = {"IDLE", "DELETED", "FAILED"}


class Cli:
    def __init__(self, binary: str, profile: str | None, dry_run: bool) -> None:
        self.binary, self.profile, self.dry_run = binary, profile, dry_run
        self.failures: list[str] = []

    def _base(self, args: list[str]) -> list[str]:
        cmd = [self.binary, *args]
        if self.profile:
            cmd += ["--profile", self.profile]
        return cmd

    def read(self, *args: str):
        """Read-only calls always run, even in dry-run, to list what's there."""
        out = subprocess.run(self._base([*args, "-o", "json"]), capture_output=True, text=True, cwd=REPO_ROOT)
        if out.returncode != 0:
            self.failures.append(f"{' '.join(args)}: {out.stderr.strip()[:300]}")
            return None
        text = out.stdout.strip()
        return json.loads(text) if text else None

    def act(self, description: str, *args: str) -> None:
        if self.dry_run:
            print(f"  would {description}")
            return
        out = subprocess.run(self._base(list(args)), capture_output=True, text=True, cwd=REPO_ROOT)
        if out.returncode == 0:
            print(f"  {description}: done")
        else:
            message = f"{description}: FAILED - {out.stderr.strip()[:300]}"
            print(f"  {message}")
            self.failures.append(message)


def _items(payload, key: str) -> list[dict]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    return payload.get(key) or []


def stop_jobs(cli: Cli) -> None:
    print("Jobs:")
    jobs = _items(cli.read("jobs", "list"), "jobs")
    if not jobs:
        print("  none")
    for job in jobs:
        job_id = str(job["job_id"])
        name = job.get("settings", {}).get("name", job_id)
        cli.act(f"cancel active runs of '{name}'", "jobs", "cancel-all-runs", "--job-id", job_id)
        schedule = (cli.read("jobs", "get", job_id) or {}).get("settings", {}).get("schedule")
        if schedule and schedule.get("pause_status") != "PAUSED":
            paused = {**schedule, "pause_status": "PAUSED"}
            body = json.dumps({"new_settings": {"schedule": paused}})
            cli.act(f"pause the schedule of '{name}'", "jobs", "update", job_id, "--json", body)


def stop_pipelines(cli: Cli) -> None:
    print("Pipelines:")
    pipelines = _items(cli.read("pipelines", "list-pipelines"), "statuses")
    if not pipelines:
        print("  none")
    for pipeline in pipelines:
        name, state = pipeline.get("name", pipeline["pipeline_id"]), pipeline.get("state", "")
        if state in IDLE_PIPELINE_STATES:
            print(f"  '{name}' already {state}")
        else:
            cli.act(f"stop pipeline '{name}' ({state})", "pipelines", "stop", pipeline["pipeline_id"])


def stop_warehouses(cli: Cli) -> None:
    print("SQL warehouses:")
    warehouses = _items(cli.read("warehouses", "list"), "warehouses")
    if not warehouses:
        print("  none")
    for warehouse in warehouses:
        name, state = warehouse.get("name", warehouse["id"]), warehouse.get("state", "")
        if state in IDLE_WAREHOUSE_STATES:
            print(f"  '{name}' already {state}")
        else:
            cli.act(f"stop warehouse '{name}' ({state})", "warehouses", "stop", warehouse["id"])


def stop_clusters(cli: Cli) -> None:
    print("Clusters:")
    clusters = _items(cli.read("clusters", "list"), "clusters")
    running = [c for c in clusters if c.get("state") in RUNNING_CLUSTER_STATES]
    if not running:
        print("  none running")
    for cluster in running:
        name = cluster.get("cluster_name", cluster["cluster_id"])
        cli.act(f"terminate cluster '{name}' ({cluster['state']})", "clusters", "delete", cluster["cluster_id"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="list what would be stopped; change nothing")
    parser.add_argument("--profile", default=None, help="Databricks CLI profile (default: the CLI default)")
    parser.add_argument(
        "--destroy",
        action="store_true",
        help="also remove this project's deployed jobs and pipelines (irreversible)",
    )
    args = parser.parse_args(argv)

    binary = shutil.which("databricks")
    if binary is None:
        print("Databricks CLI not found on PATH.", file=sys.stderr)
        return 2

    cli = Cli(binary, args.profile, args.dry_run)
    print("DRY RUN - nothing will be changed\n" if args.dry_run else "Stopping everything that can cost money\n")
    stop_jobs(cli)
    stop_pipelines(cli)
    stop_warehouses(cli)
    stop_clusters(cli)
    if args.destroy:
        print("Bundle:")
        cli.act("destroy this project's deployed resources", "bundle", "destroy", "-t", "dev", "--auto-approve")

    if cli.failures:
        print(f"\n{len(cli.failures)} problem(s):", file=sys.stderr)
        for failure in cli.failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print("\nDone." if not args.dry_run else "\nDry run complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
