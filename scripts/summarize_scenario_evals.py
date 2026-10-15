"""Collects scenario-eval reports (evals/run.py, gitignored) into one
committed comparison: per model, which of the 14 scenarios passed and why the
others failed. A run where the model call itself failed (the agent's
graceful-degradation answer) is marked `model_unavailable`, not counted as
the model's mistake.

    uv run python scripts/summarize_scenario_evals.py evals/reports/<a>.json evals/reports/<b>.json ...

Writes docs/comparisons/results/scenario_evals_<date>.json.
"""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from agent.orchestrator import AGENT_UNAVAILABLE_ANSWER  # noqa: E402

OUT_DIR = REPO / "docs" / "comparisons" / "results"


def summarize(report: dict) -> dict:
    """Per scenario: passed only if every valid run passed (a report made
    with --repeat has several runs per scenario)."""
    scenarios = {}
    for r in report["results"]:
        unavailable = r.get("answer") == AGENT_UNAVAILABLE_ANSWER
        s = scenarios.setdefault(
            r["id"], {"runs": 0, "passed_runs": 0, "model_unavailable": 0, "failures": [], "tools_called": []}
        )
        if unavailable:
            s["model_unavailable"] += 1
            continue
        s["runs"] += 1
        s["passed_runs"] += r["passed"]
        s["failures"] += [f for f in r["failures"] if f not in s["failures"]]
        s["tools_called"] = r["tools_called"]
    for s in scenarios.values():
        s["passed"] = s["runs"] > 0 and s["passed_runs"] == s["runs"]
        s["model_unavailable"] = s["runs"] == 0
    valid = [s for s in scenarios.values() if not s["model_unavailable"]]
    return {
        "model": report["model"],
        "passed": sum(s["passed"] for s in valid),
        "valid_runs": len(valid),
        "runs_per_scenario": max((s["runs"] for s in valid), default=0),
        "runs_passed": sum(s["passed_runs"] for s in valid),
        "runs_total": sum(s["runs"] for s in valid),
        "scenarios": scenarios,
    }


def main(argv: list[str]) -> int:
    models = [summarize(json.loads(Path(p).read_text(encoding="utf-8"))) for p in argv]
    out = OUT_DIR / f"scenario_evals_{datetime.now(UTC):%Y%m%d}.json"
    out.write_text(
        json.dumps(
            {
                "what": "evals/scenarios.yaml (14 scenarios) with Databricks-hosted models as the planner",
                "workspace": "Databricks Free Edition (Foundation Model APIs, no paid key)",
                "max_turns": 6,
                "models": models,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    for m in models:
        print(f"{m['model']}: {m['passed']}/{m['valid_runs']} ({m['runs_passed']}/{m['runs_total']} runs)")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
