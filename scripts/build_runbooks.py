"""Generates one runbook page per problem in agent/knowledge/problem_catalog.yaml
(docs/runbooks/<ID>.md), the documents behind the Agent Bricks Knowledge
Assistant (scripts/runbook_assistant.py). The catalog stays the single
source of truth; these pages are a readable rendering of it.

    uv run python scripts/build_runbooks.py           # write docs/runbooks/
    uv run python scripts/build_runbooks.py --check   # fail if they're out of date (CI)
"""

import argparse
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
CATALOG = REPO / "agent" / "knowledge" / "problem_catalog.yaml"
OUT = REPO / "docs" / "runbooks"

LEVELS = {
    "L0": "observe and report only",
    "L1": "recommend and escalate to a person; the agent changes nothing",
    "L2": "act within guardrails - destructive actions wait for a named approver",
    "L3": "enforced in code on every request; no judgment call",
}
STATUS = {
    "exists": "a working tool or check exists in this project",
    "partial": "partly built",
    "to_build": "not built yet - handle by hand",
}


def _level_text(level: str) -> str:
    parts = [p.strip() for p in str(level).split("-")]
    if len(parts) == 2 and all(p.startswith("L") or p.isdigit() for p in parts):
        parts = [parts[0], parts[1] if parts[1].startswith("L") else "L" + parts[1]]
    return "; ".join(f"{p}: {LEVELS[p]}" for p in parts if p in LEVELS) or str(level)


def page(problem: dict, layer: dict) -> str:
    pid = problem["id"]
    lines = [
        f"# {pid}: {problem['title']}",
        "",
        f"Problem ID: {pid}. Layer {layer['code']}: {layer['title']}.",
        "",
        "## What happens",
        "",
        problem["description"],
        "",
        "## Who it affects",
        "",
        {"DE": "Data engineers", "DA": "Data analysts", "DE + DA": "Data engineers and data analysts"}.get(
            problem["who"], problem["who"]
        )
        + ".",
        "",
        "## How to detect it",
        "",
        problem["detect"],
        "",
        "## What to do",
        "",
        problem["action"],
        "",
        "## Autonomy",
        "",
        f"Level {problem['level']} ({_level_text(problem['level'])}).",
        "",
        "## Status in this project",
        "",
        f"{STATUS.get(problem['status'], problem['status']).capitalize()}."
        + (f" {problem['status_note']}" if problem.get("status_note") else ""),
        "",
    ]
    return "\n".join(lines)


def pages() -> dict[str, str]:
    catalog = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
    return {
        f"{problem['id']}.md": page(problem, layer) for layer in catalog["layers"] for problem in layer["problems"]
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    wanted = pages()
    if args.check:
        current = {p.name: p.read_text(encoding="utf-8") for p in OUT.glob("*.md")} if OUT.exists() else {}
        if current != wanted:
            stale = sorted(set(wanted) ^ set(current) | {k for k in wanted if current.get(k) != wanted[k]})
            print(f"runbooks out of date ({len(stale)}): {stale[:10]} - run scripts/build_runbooks.py")
            return 1
        print(f"{len(wanted)} runbooks up to date")
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.md"):
        if old.name not in wanted:
            old.unlink()
    for name, text in wanted.items():
        (OUT / name).write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {len(wanted)} runbooks to {OUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
