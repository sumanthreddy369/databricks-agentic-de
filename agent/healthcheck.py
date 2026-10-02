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

STATUS: the CLI is tested locally with a scripted fake Claude
(tests/test_healthcheck.py). The scheduled job itself has never run on a
real workspace.
"""

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from agent.orchestrator import AGENT_UNAVAILABLE_ANSWER, DEFAULT_AUDIT_LOG_PATH, OrchestratorAgent

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
    args = parser.parse_args(argv)

    state_path = Path(args.state_path)
    _ensure_state_file(state_path)
    if agent is None:
        agent = OrchestratorAgent(state_path=state_path, audit_log_path=args.audit_log_path)

    result = agent.handle(HEALTHCHECK_REQUEST, mode="de")
    print(json.dumps(asdict(result)))

    if result.answer == AGENT_UNAVAILABLE_ANSWER:
        return EXIT_AGENT_UNAVAILABLE
    if "notify_and_page" in result.tool_calls:
        return EXIT_ESCALATED
    if result.pending_approvals:
        return EXIT_APPROVAL_PENDING
    return EXIT_OK


def main() -> None:
    # Console-script and python_wheel_task entry point. sys.exit (rather
    # than returning the code) is what makes a non-zero code fail the task.
    sys.exit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
