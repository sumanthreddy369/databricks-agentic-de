"""Builds each scenario's isolated world and runs the orchestrator in it.

Every scenario gets its own temporary directory: a pipeline state file (the
example state with the scenario's overrides deep-merged in), a DuckDB Gold
stand-in seeded from data/seed/gold_seed.sql plus the scenario's extra SQL,
and a fresh audit log. Nothing is shared between scenarios except the MCP
server subprocess, which is stateless (every DE tool receives its state file
path explicitly).

The Claude client is injected: evals/run.py passes a real one; the test
suite passes scripted fakes to prove the harness and graders work.
"""

import copy
import json
import tempfile
from pathlib import Path

import yaml

from agent.mcp_bridge import MCPToolBridge
from agent.orchestrator import OrchestratorAgent
from evals.grading import grade

REPO_ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_PATH = Path(__file__).resolve().parent / "scenarios.yaml"
EXAMPLE_STATE = REPO_ROOT / "data" / "state" / "pipeline_state.example.json"
BASE_SEED_SQL = REPO_ROOT / "data" / "seed" / "gold_seed.sql"


def load_scenarios(path: Path = SCENARIOS_PATH) -> list[dict]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["scenarios"]


def _deep_merge(base: dict, overrides: dict) -> dict:
    merged = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def build_world(scenario: dict, workdir: Path) -> dict:
    """Writes the scenario's state file and seed SQL into `workdir` and
    returns the paths the orchestrator should use."""
    setup = scenario.get("setup") or {}
    state = _deep_merge(json.loads(EXAMPLE_STATE.read_text()), setup.get("state") or {})
    state_path = workdir / "pipeline_state.json"
    state_path.write_text(json.dumps(state, indent=2))

    seed_path = workdir / "gold_seed.sql"
    seed_path.write_text(BASE_SEED_SQL.read_text() + "\n" + (setup.get("seed_sql") or ""))
    return {
        "state_path": state_path,
        "seed_sql_path": seed_path,
        "duckdb_path": workdir / "gold.duckdb",
        "audit_log_path": workdir / "audit_log.jsonl",
    }


def run_scenario(scenario: dict, claude, *, mcp_bridge: MCPToolBridge | None = None) -> dict:
    """Runs one scenario end to end and grades it. Returns a JSON-safe dict."""
    with tempfile.TemporaryDirectory(prefix=f"eval-{scenario['id']}-") as tmp:
        world = build_world(scenario, Path(tmp))
        agent = OrchestratorAgent(claude=claude, mcp_bridge=mcp_bridge, **world)
        result = agent.handle(scenario["request"], mode=scenario.get("mode", "auto"))
        audit_path = world["audit_log_path"]
        entries = (
            [json.loads(line) for line in audit_path.read_text().splitlines() if line.strip()]
            if audit_path.exists()
            else []
        )
    failures = grade(scenario.get("expect") or {}, result, entries)
    return {
        "id": scenario["id"],
        "problems": scenario.get("problems", []),
        "passed": not failures,
        "failures": failures,
        "mode": result.mode,
        "tools_called": [entry["tool"] for entry in entries],
        "answer": result.answer,
    }
