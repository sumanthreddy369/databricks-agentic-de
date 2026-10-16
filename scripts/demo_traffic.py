"""Drives demo traffic through the REAL orchestrator, guardrails and approval
queue, so the observability stack has something to show without a paid model.

    uv run python scripts/demo_traffic.py [--rounds 3]

Only the model is scripted: each request's tool choices are fixed here
(the same pattern the tests use), so no Anthropic API call is made. All
else is the production path: masking, identifier refusal, small-group
suppression, the injection scan, the MCP tool server, the approval queue
and the audit log. Analyst questions run on the local DuckDB Gold seed;
Genie is not called (Databricks variables are cleared).

Writes to data/state/demo/ (gitignored) - never the real state or audit log.
Point the telemetry exporter at it:

    uv run python -m agent.telemetry --audit-log-path data/state/demo/audit_log.jsonl \\
        --state-path data/state/demo/pipeline_state.json

The numbers this produces describe demo traffic, not real usage.
"""

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

DEMO_DIR = REPO / "data" / "state" / "demo"
EXAMPLE_STATE = REPO / "data" / "state" / "pipeline_state.example.json"
APPROVER = "on-call DE (demo)"

ANALYST_REQUESTS = [
    (
        "How many patients are in the ICU?",
        [("compute_metric", {"metric": "current_census", "filters": {"unit": "ICU"}})],
    ),
    ("What is the census by unit?", [("compute_metric", {"metric": "current_census", "group_by": ["unit"]})]),
    (
        "Census by unit and encounter type?",
        [("compute_metric", {"metric": "current_census", "group_by": ["unit", "encounter_type"]})],
    ),
    (
        "What is the full name of patient pt_00052?",
        [("ask_genie", {"question": "What is the full name of patient pt_00052?"})],
    ),
    (
        "List the MRNs of patients in the ED.",
        [("ask_genie", {"question": "List the MRNs of patients in the ED."})],
    ),
    (
        "How many providers per specialty?",
        [("compute_metric", {"metric": "provider_count", "group_by": ["specialty"]})],
    ),
]


class ScriptedModel:
    """Stands in for agent.llm.Claude: dispatches a fixed tool sequence."""

    runs_locally = True  # no model: fine under AGENT_MODEL_POLICY=local_only

    def __init__(self, steps, answer="Done (scripted demo)."):
        self.steps, self.answer = steps, answer

    def run_tool_loop(self, system, messages, tools, dispatch, max_turns=6):
        for name, tool_input in self.steps:
            dispatch(name, tool_input)
        return self.answer


def _agent(steps, state_path: Path, audit_path: Path):
    from agent.orchestrator import OrchestratorAgent

    return OrchestratorAgent(claude=ScriptedModel(steps), state_path=state_path, audit_log_path=audit_path)


def run(rounds: int, demo_dir: Path = DEMO_DIR) -> dict:
    from simulator.chaos import simulate_pipeline_failure

    for var in ("DATABRICKS_HOST", "DATABRICKS_TOKEN", "GENIE_SPACE_ID", "DATABRICKS_WAREHOUSE_ID"):
        os.environ.pop(var, None)
    demo_dir.mkdir(parents=True, exist_ok=True)
    state_path, audit_path = demo_dir / "pipeline_state.json", demo_dir / "audit_log.jsonl"
    if not state_path.exists():
        state = json.loads(EXAMPLE_STATE.read_text(encoding="utf-8"))
        state["remediation_approval"] = "required"  # people approve fixes, as in production
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    counts = {"analyst": 0, "health_checks": 0, "approved": 0, "rejected": 0}
    for i in range(rounds):
        for _question, steps in ANALYST_REQUESTS:
            _agent(steps, state_path, audit_path).handle(_question, mode="da")
            counts["analyst"] += 1

        _agent([("check_expectation_metrics", {}), ("check_job_status", {})], state_path, audit_path).handle(
            "Routine health check", mode="de"
        )

        simulate_pipeline_failure(state_path, "silver_vitals", "plausible_vital_value", failure_count=5 + i)
        incident = _agent(
            [
                ("check_expectation_metrics", {}),
                ("lookup_problem", {"query": "silver_vitals plausible_vital_value failing, impossible values"}),
                ("quarantine_bad_records", {"table": "silver_vitals", "expectation": "plausible_vital_value"}),
                ("restart_pipeline", {"pipeline_name": "streaming_patient_pipeline"}),
            ],
            state_path,
            audit_path,
        ).handle("Health check: vitals expectation failing", mode="de")

        simulate_pipeline_failure(state_path, "silver_encounters", "known_event_type", failure_count=1)
        _agent(
            [
                ("check_expectation_metrics", {}),
                ("lookup_problem", {"query": "unknown event type in silver_encounters - contract break"}),
                ("notify_and_page", {"message": "Contract break: unknown event_type in silver_encounters"}),
            ],
            state_path,
            audit_path,
        ).handle("Health check: contract break", mode="de")
        counts["health_checks"] += 3

        # A person decides: approve the quarantine, reject the restart.
        approver = _agent([], state_path, audit_path)
        for approval_id in incident.pending_approvals:
            pending = next(
                r
                for r in json.loads(state_path.read_text(encoding="utf-8"))["pending_approvals"]
                if r["id"] == approval_id
            )
            if pending["status"] != "pending":
                continue
            if pending["tool"] == "quarantine_bad_records":
                approver.execute_approved(approval_id, APPROVER)
                counts["approved"] += 1
            else:
                approver.reject_approval(
                    approval_id, APPROVER, reason="restart not needed after quarantine (demo)"
                )
                counts["rejected"] += 1

        # Reset expectation counters so the next round starts healthy.
        state = json.loads(state_path.read_text(encoding="utf-8"))
        for table in state["tables"].values():
            for expectation in table["expectations"].values():
                expectation["failure_count"] = 0
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--reset", action="store_true", help="delete data/state/demo first")
    args = parser.parse_args(argv)
    if args.reset and DEMO_DIR.exists():
        shutil.rmtree(DEMO_DIR)
    counts = run(args.rounds)  # spans, if OTEL_EXPORTER_OTLP_ENDPOINT is set, flush at exit
    print(json.dumps(counts))
    print(f"audit log: {DEMO_DIR / 'audit_log.jsonl'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
