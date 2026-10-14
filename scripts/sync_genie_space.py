"""Pushes governance/genie/space.json to the live Genie space.

    uv run python scripts/build_genie_space.py --check     # make sure it's current
    uv run python scripts/sync_genie_space.py <space_id>   # or set GENIE_SPACE_ID

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
from pathlib import Path

SPACE_PATH = Path(__file__).resolve().parent.parent / "governance" / "genie" / "space.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("space_id", nargs="?", default=os.environ.get("GENIE_SPACE_ID"))
    args = parser.parse_args(argv)
    if not args.space_id:
        print("give a space id or set GENIE_SPACE_ID", file=sys.stderr)
        return 2
    cli = shutil.which("databricks")
    if cli is None:
        print("Databricks CLI not found on PATH", file=sys.stderr)
        return 2

    spec = json.loads(SPACE_PATH.read_text(encoding="utf-8"))
    body = {
        "serialized_space": json.dumps(spec["space"]),
        "title": spec["title"],
        "description": spec["description"],
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(body, f)
    out = subprocess.run(
        [cli, "genie", "update-space", args.space_id, "--json", f"@{f.name}", "-o", "json"],
        capture_output=True,
        text=True,
        cwd=tempfile.gettempdir(),
    )
    Path(f.name).unlink(missing_ok=True)
    if out.returncode != 0:
        print(out.stderr.strip()[:1000], file=sys.stderr)
        return 1
    print(f"updated Genie space {args.space_id} from {SPACE_PATH.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
