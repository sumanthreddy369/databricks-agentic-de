"""Runs the agent under AGENT_MODEL_POLICY=local_only with the laptop model and
records every network connection the agent process opens (Python's
`socket.connect` audit hook), to show where its traffic actually goes:

    uv run python scripts/egress_check.py [--model qwen3:14b]

One analyst question (local DuckDB Gold) and one health check (local state
file), so no workspace is involved. Scope: this process - the planner and
the in-process analyst tools. The MCP tool server is a child process; with
local state and RUNBOOK_SEARCH unset or local it makes no network calls.

Writes docs/comparisons/results/egress_check_<date>.json.
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

CONNECTIONS: list[str] = []


def _audit(event: str, args) -> None:
    if event == "socket.connect":
        address = args[1]
        if isinstance(address, tuple) and len(address) >= 2:
            CONNECTIONS.append(f"{address[0]}:{address[1]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--model", default="qwen3:14b")
    args = parser.parse_args(argv)
    for name in ("DATABRICKS_HOST", "DATABRICKS_TOKEN", "DATABRICKS_WAREHOUSE_ID", "ANTHROPIC_API_KEY"):
        os.environ.pop(name, None)
    os.environ["AGENT_MODEL_POLICY"] = "local_only"
    os.environ["AGENT_LOCAL_MODEL"] = args.model
    sys.addaudithook(_audit)

    from agent.orchestrator import OrchestratorAgent

    work = Path(tempfile.mkdtemp(prefix="egress_"))
    state = work / "state.json"
    shutil.copy(REPO / "data" / "state" / "pipeline_state.example.json", state)
    agent = OrchestratorAgent(
        state_path=state, duckdb_path=work / "gold.duckdb", audit_log_path=work / "audit.jsonl"
    )
    runs = []
    for request, mode in (
        ("How many patients are currently in the ICU?", "da"),
        ("Scheduled pipeline health check.", "de"),
    ):
        before = len(CONNECTIONS)
        result = agent.handle(request, mode=mode)
        runs.append(
            {
                "request": request,
                "mode": mode,
                "planner": type(agent.claude).__name__ + f" ({agent.claude.model})",
                "tool_calls": result.tool_calls,
                "answer": result.answer[:300],
                "connections": dict(Counter(CONNECTIONS[before:])),
            }
        )
    destinations = sorted(set(CONNECTIONS))
    local = {"127.0.0.1", "::1", "localhost"}
    report = {
        "policy": "local_only",
        "runs": runs,
        "all_destinations": destinations,
        "only_this_machine": all(d.rsplit(":", 1)[0].strip("[]") in local for d in destinations),
    }
    out = REPO / "docs" / "comparisons" / "results" / f"egress_check_{datetime.now(UTC):%Y%m%d}.json"
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("all_destinations", "only_this_machine")}, indent=1))
    print(f"wrote {out}")
    shutil.rmtree(work, ignore_errors=True)
    return 0 if report["only_this_machine"] else 1


if __name__ == "__main__":
    sys.exit(main())
