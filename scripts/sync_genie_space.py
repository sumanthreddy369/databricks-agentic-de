"""Pushes governance/genie/space.json to the live Genie space.

    uv run python scripts/build_genie_space.py --check     # make sure it's current
    uv run python scripts/sync_genie_space.py <space_id>   # or set GENIE_SPACE_ID
    # find the space by its title: update it, or create it on the warehouse
    uv run python scripts/sync_genie_space.py --upsert --warehouse-id <id> [--profile free]

Full replacement of the space's instructions, example SQL, sample
questions and tables with the ones generated from the metrics layer. Uses
the Databricks CLI and its login, so it needs no token of its own.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SPACE_PATH = Path(__file__).resolve().parent.parent / "governance" / "genie" / "space.json"


def _cli(args: list[str], profile: str | None) -> subprocess.CompletedProcess:
    cli = shutil.which("databricks")
    if cli is None:
        raise SystemExit("Databricks CLI not found on PATH")
    cmd = [cli, *args, "-o", "json"] + (["--profile", profile] if profile else [])
    # Run outside the repo so the CLI doesn't pick up the bundle's target.
    return subprocess.run(cmd, capture_output=True, text=True, cwd=tempfile.gettempdir())


def _json(text: str) -> dict:
    """CLI JSON output, skipping any warning lines printed before it."""
    return json.loads(text[text.index("{") :]) if "{" in text else {}


def find_space(title: str, profile: str | None) -> str | None:
    token = None
    while True:
        out = _cli(["genie", "list-spaces"] + (["--page-token", token] if token else []), profile)
        if out.returncode != 0:
            raise SystemExit(out.stderr.strip()[:500])
        page = _json(out.stdout)
        for space in page.get("spaces", []):
            if space.get("title") == title:
                return space["space_id"]
        token = page.get("next_page_token")
        if not token:
            return None


def push(spec: dict, profile: str | None, *, space_id: str | None = None, warehouse_id: str | None = None) -> str:
    """Updates `space_id`, or creates a space on `warehouse_id`; returns the id."""
    body = {
        "serialized_space": json.dumps(spec["space"]),
        "title": spec["title"],
        "description": spec["description"],
    }
    if space_id is None:
        body["warehouse_id"] = warehouse_id
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(body, f)
    try:
        if space_id:
            out = _cli(["genie", "update-space", space_id, "--json", f"@{f.name}"], profile)
        else:
            out = _cli(["genie", "create-space", "--json", f"@{f.name}"], profile)
    finally:
        Path(f.name).unlink(missing_ok=True)
    if out.returncode != 0:
        raise SystemExit(out.stderr.strip()[:1000])
    return space_id or _json(out.stdout)["space_id"]


def upsert(profile: str | None, warehouse_id: str) -> tuple[str, str]:
    """(space_id, "updated" | "created") for the space titled as in space.json."""
    spec = json.loads(SPACE_PATH.read_text(encoding="utf-8"))
    existing = find_space(spec["title"], profile)
    space_id = push(spec, profile, space_id=existing, warehouse_id=warehouse_id)
    if not existing:
        # The list is eventually consistent (a new space appeared ~5 s after
        # creation on Free Edition); wait so a quick re-run finds it instead of
        # creating a duplicate.
        for _ in range(12):
            if find_space(spec["title"], profile) == space_id:
                break
            time.sleep(5)
    return space_id, "updated" if existing else "created"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("space_id", nargs="?", default=os.environ.get("GENIE_SPACE_ID"))
    parser.add_argument("--upsert", action="store_true", help="find the space by title; create it if missing")
    parser.add_argument("--warehouse-id", help="SQL warehouse for a new space (--upsert)")
    parser.add_argument("--profile", default=None, help="Databricks CLI profile")
    args = parser.parse_args(argv)
    if args.upsert:
        if not args.warehouse_id:
            print("--upsert needs --warehouse-id", file=sys.stderr)
            return 2
        space_id, action = upsert(args.profile, args.warehouse_id)
        print(f"{action} Genie space {space_id} from {SPACE_PATH.name}")
        return 0
    if not args.space_id:
        print("give a space id, set GENIE_SPACE_ID, or use --upsert", file=sys.stderr)
        return 2
    push(json.loads(SPACE_PATH.read_text(encoding="utf-8")), args.profile, space_id=args.space_id)
    print(f"updated Genie space {args.space_id} from {SPACE_PATH.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
