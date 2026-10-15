"""Entry point for the scheduled DE-mode pipeline health check — the
`orchestrator_healthcheck` console script that
resources/workflows.yml:agent_pipeline_healthcheck runs as a
`python_wheel_task` every 15 minutes.

One run = one `OrchestratorAgent.handle(..., mode="de")` call with a fixed
request, then a JSON summary on stdout (which lands in the job run's
output) and an exit code the job's status reflects:

- 0: the agent ran and did not need a human.
- 1: the agent couldn't run at all (the Claude call path failed and
  `handle()` degraded gracefully). The DLT expectations still enforce data
  quality, but nobody is watching the pipeline, so the run must not look
  green.
- 2: the agent escalated via `notify_and_page`. That tool only records an
  incident in the state file (no PagerDuty/Slack integration exists yet), so
  a failed job run is the one alert channel a human is guaranteed to see,
  via the job's failure notifications.
- 3: a remediation is waiting for human approval (agent/approvals.py). Nothing
  was changed; the failed run is what tells a person to decide.

The state file (kill switch, escalation-ceiling counters, incidents) and the
audit log must persist across runs, so on Databricks both point at a Unity
Catalog volume (`PIPELINE_STATE_PATH` / `AUDIT_LOG_PATH` in
resources/workflows.yml). A missing state file is initialized with
remediation enabled and no history, which is what a first run needs.

Planner: Claude when ANTHROPIC_API_KEY is set, otherwise the deterministic
rules planner (agent/rules_planner.py) - same tools, guardrails, approval
gate and audit log, decisions taken from the problem catalog's autonomy
levels. `--planner` or HEALTHCHECK_PLANNER overrides; `--planner databricks`
uses a Databricks-hosted model (`--model-endpoint`, agent/databricks_llm.py)
with the run's own credentials - no paid key.
"""

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from agent.databricks_client import load_config
from agent.orchestrator import AGENT_UNAVAILABLE_ANSWER, DEFAULT_AUDIT_LOG_PATH, OrchestratorAgent
from agent.rules_planner import RulesPlanner

DEFAULT_STATE_PATH = "data/state/pipeline_state.json"

HEALTHCHECK_REQUEST = (
    "Scheduled pipeline health check: check expectation metrics, job status, and schema drift. "
    "Remediate anything auto-fixable; escalate anything that isn't."
)

EXIT_OK = 0
EXIT_AGENT_UNAVAILABLE = 1
EXIT_ESCALATED = 2
EXIT_APPROVAL_PENDING = 3

_INITIAL_STATE = {
    "autonomous_remediation_enabled": True,
    # Real deployments start with every remediation waiting for a person.
    "remediation_approval": "required",
    "remediation_attempts": {},
    "incidents": [],
}


def _ensure_state_file(state_path: Path) -> None:
    if state_path.exists():
        return
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(_INITIAL_STATE, indent=2))


def run(argv: list[str] | None = None, *, agent: OrchestratorAgent | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orchestrator_healthcheck", description=__doc__.split("\n\n")[0])
    parser.add_argument("--state-path", default=os.environ.get("PIPELINE_STATE_PATH", DEFAULT_STATE_PATH))
    parser.add_argument("--audit-log-path", default=os.environ.get("AUDIT_LOG_PATH", DEFAULT_AUDIT_LOG_PATH))
    parser.add_argument(
        "--planner",
        choices=["claude", "rules", "databricks"],
        default=os.environ.get("HEALTHCHECK_PLANNER")
        or ("claude" if os.environ.get("ANTHROPIC_API_KEY") else "rules"),
        help="claude (needs ANTHROPIC_API_KEY), rules (deterministic, agent/rules_planner.py; the default "
        "without a key) or databricks (a Model Serving endpoint, --model-endpoint)",
    )
    parser.add_argument(
        "--model-endpoint",
        default=os.environ.get("HEALTHCHECK_MODEL_ENDPOINT", "databricks-qwen35-122b-a10b"),
        help="Model Serving endpoint for --planner databricks",
    )
    parser.add_argument(
        "--pipeline-ids",
        default=None,
        help='JSON {"name": "pipeline_id"} for the live tools (serverless jobs pass this instead of an env var)',
    )
    parser.add_argument(
        "--alert-webhook-secret",
        default=None,
        help="Databricks secret scope/key holding the alert webhook URL (read with the run's own identity)",
    )
    args = parser.parse_args(argv)
    if args.planner == "rules":
        # The rules planner acts on lookup_problem's top match, so it keeps the
        # deterministic keyword ranking: with semantic ranking on, the finding
        # "plausible_vital_value failing in silver_vitals" ranked M4 (page)
        # above B1 (approval-gated quarantine) in a live run.
        os.environ.pop("RUNBOOK_SEARCH", None)
    if args.pipeline_ids:
        os.environ["DATABRICKS_PIPELINE_IDS"] = args.pipeline_ids
    if args.alert_webhook_secret and not os.environ.get("ALERT_WEBHOOK_URL"):
        url = _read_databricks_secret(args.alert_webhook_secret)
        if url:
            os.environ["ALERT_WEBHOOK_URL"] = url  # forwarded to the tool server; never printed

    # Inside a Databricks job the run's own identity is only reachable from
    # this process; the MCP tool server is a child process without that
    # runtime context. Resolve it here and pass it on through the environment
    # the bridge forwards (agent/mcp_bridge.py). Never printed or logged.
    config = load_config()
    if config is not None and not os.environ.get("DATABRICKS_TOKEN"):
        os.environ["DATABRICKS_HOST"] = config.host
        os.environ["DATABRICKS_TOKEN"] = config.token

    state_path = Path(args.state_path)
    _ensure_state_file(state_path)
    if agent is None:
        if args.planner == "rules":
            planner = RulesPlanner()
        elif args.planner == "databricks":
            from agent.databricks_llm import DatabricksModel

            planner = DatabricksModel(args.model_endpoint)
        else:
            planner = None
        agent = OrchestratorAgent(claude=planner, state_path=state_path, audit_log_path=args.audit_log_path)

    before = _read_state(state_path)
    known_incidents = len(before.get("incidents", []))
    known_approvals = {a["id"] for a in before.get("pending_approvals", []) if a.get("status") == "pending"}

    result = agent.handle(HEALTHCHECK_REQUEST, mode="de")
    print(json.dumps(asdict(result)))

    if result.answer == AGENT_UNAVAILABLE_ANSWER:
        return EXIT_AGENT_UNAVAILABLE
    # A failed run is an alert (the job emails its owner), so fail only for
    # something new. On a 15-minute schedule an unresolved, already-reported
    # problem would otherwise email every run until someone fixes it.
    # Stay quiet only when the state positively shows every incident raised
    # this run was a repeat; anything unclear fails loud.
    added = _read_state(state_path).get("incidents", [])[known_incidents:]
    all_repeats = bool(added) and all((i.get("alert") or {}).get("new") is False for i in added)
    if "notify_and_page" in result.tool_calls and not all_repeats:
        return EXIT_ESCALATED
    if set(result.pending_approvals) - known_approvals:
        return EXIT_APPROVAL_PENDING
    if "notify_and_page" in result.tool_calls or result.pending_approvals:
        print(json.dumps({"note": "known problems only: already alerted / already waiting for approval"}))
    return EXIT_OK


def _read_databricks_secret(scope_key: str) -> str | None:
    """scope/key from Databricks secrets, or None (missing secret, not on
    Databricks): alerts then fall back to the job's failure email."""
    scope, _, key = scope_key.partition("/")
    try:
        from databricks.sdk import WorkspaceClient

        return WorkspaceClient().dbutils.secrets.get(scope=scope, key=key) or None
    except Exception:  # noqa: BLE001 - a missing alert channel must never stop the health check
        print(json.dumps({"note": f"alert webhook secret {scope_key} not available; no webhook alerts"}))
        return None


def _read_state(state_path: Path) -> dict:
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def main() -> None:
    # Console-script and python_wheel_task entry point. sys.exit (rather
    # than returning the code) is what makes a non-zero code fail the task -
    # but only for non-zero: serverless wheel tasks run under IPython, which
    # reports even SystemExit(0) as a failed run (seen on Free Edition).
    code = run(sys.argv[1:])
    if code:
        sys.exit(code)


if __name__ == "__main__":
    main()
