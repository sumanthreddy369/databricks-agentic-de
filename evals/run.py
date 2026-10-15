"""Runs evals/scenarios.yaml against the real Claude model and reports pass
rates. This spends real API tokens - every scenario is one full agent run.

    uv run python -m evals.run                         # all scenarios
    uv run python -m evals.run --scenario da-c5-small-group-is-suppressed
    uv run python -m evals.run --model claude-opus-5-5 --repeat 3
    # a Databricks-hosted model instead (Free Edition: no paid key)
    uv run python -m evals.run --databricks-profile free --databricks-endpoint databricks-llama-4-maverick

Needs Anthropic credentials (ANTHROPIC_API_KEY, or an `ant auth login`
profile), or with --databricks-endpoint a Databricks CLI profile. Live
Databricks settings are cleared for the run so every scenario uses its own
local state file and DuckDB, never a real workspace. A JSON
report is written to evals/reports/ (gitignored).
"""

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from agent.databricks_llm import DatabricksModel
from agent.llm import Claude
from agent.mcp_bridge import MCPToolBridge
from evals.harness import load_scenarios, run_scenario

REPORTS_DIR = Path(__file__).resolve().parent / "reports"
_LIVE_ENV = ("DATABRICKS_HOST", "DATABRICKS_TOKEN", "DATABRICKS_WAREHOUSE_ID", "DATABRICKS_PIPELINE_IDS")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.run", description="Run the scenario evals against Claude.")
    parser.add_argument("--scenario", action="append", help="scenario id (repeatable); default: all")
    parser.add_argument("--model", default=os.environ.get("CLAUDE_MODEL"), help="model ID; default: agent default")
    parser.add_argument("--repeat", type=int, default=1, help="runs per scenario, to measure consistency")
    parser.add_argument("--databricks-endpoint", help="a Model Serving endpoint as the planner, instead of Claude")
    parser.add_argument("--databricks-profile", help="Databricks CLI profile for that endpoint (e.g. free)")
    args = parser.parse_args(argv)

    if args.databricks_endpoint:
        # Before the live settings are cleared: the model's credentials come
        # from the profile and stay in this process, not in the tools' env.
        claude = DatabricksModel(args.databricks_endpoint, profile=args.databricks_profile)
    for name in _LIVE_ENV:
        os.environ.pop(name, None)  # inherited by the MCP server subprocess too

    scenarios = load_scenarios()
    if args.scenario:
        wanted = set(args.scenario)
        scenarios = [s for s in scenarios if s["id"] in wanted]
        missing = wanted - {s["id"] for s in scenarios}
        if missing:
            print(f"unknown scenario ids: {sorted(missing)}", file=sys.stderr)
            return 2

    if not args.databricks_endpoint:
        claude = Claude(model=args.model) if args.model else Claude()
    bridge = MCPToolBridge()
    results = []
    try:
        for scenario in scenarios:
            for attempt in range(args.repeat):
                outcome = run_scenario(scenario, claude, mcp_bridge=bridge)
                outcome["attempt"] = attempt + 1
                results.append(outcome)
                mark = "PASS" if outcome["passed"] else "FAIL"
                print(f"{mark}  {scenario['id']}  (run {attempt + 1})")
                for failure in outcome["failures"]:
                    print(f"        - {failure}")
    finally:
        bridge.close()

    passed = sum(r["passed"] for r in results)
    print(f"\n{passed}/{len(results)} runs passed with model {claude.model}")

    REPORTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report = REPORTS_DIR / f"{stamp}.json"
    report.write_text(
        json.dumps({"model": claude.model, "passed": passed, "runs": len(results), "results": results}, indent=2)
    )
    print(f"report: {report}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
