"""Human approval CLI for queued remediations.

When remediation_approval is "required" (the default for real deployments),
the agent can only *request* quarantine_bad_records / restart_pipeline; the
request waits in the state file until a person decides here:

    uv run python -m agent.approvals list
    uv run python -m agent.approvals approve apr_1a2b3c4d5e --by "Jane Doe"
    uv run python -m agent.approvals reject  apr_1a2b3c4d5e --by "Jane Doe" --reason "known upstream issue"

Approving runs the action through the orchestrator's normal dispatch path, so
the kill switch, escalation ceiling, guardrails, and audit log all apply at
execution time, and the audit line records the approver. This module never
calls a model - the decision is a person's, not the agent's - and none of
these commands is exposed to the model as a tool.

Limitation: `--by` is a recorded name, not an authenticated identity. In a
real deployment this sits behind whatever already authenticates the person
(a Databricks job run by them, an internal approvals UI, or a chat-ops bot).
"""

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from agent.healthcheck import DEFAULT_STATE_PATH
from agent.orchestrator import DEFAULT_AUDIT_LOG_PATH, OrchestratorAgent
from agent.tools import pipeline_health


class _NoModel:
    """Approvals never involve the model; fail loudly if anything tries."""

    def run_tool_loop(self, *args, **kwargs):
        raise RuntimeError("approval decisions never call the model")


def pending(state_path: Path) -> list[dict]:
    now = datetime.now(UTC)
    state = pipeline_health.load_state(state_path)
    return [
        r
        for r in state.get("pending_approvals", [])
        if r["status"] == "pending" and datetime.fromisoformat(r["expires_at"]) > now
    ]


def run(argv: list[str] | None = None, *, mcp_bridge=None) -> int:
    parser = argparse.ArgumentParser(prog="agent.approvals", description="Approve or reject queued remediations.")
    parser.add_argument("--state-path", default=os.environ.get("PIPELINE_STATE_PATH", DEFAULT_STATE_PATH))
    parser.add_argument("--audit-log-path", default=os.environ.get("AUDIT_LOG_PATH", DEFAULT_AUDIT_LOG_PATH))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="show pending requests")
    approve = sub.add_parser("approve", help="approve and execute a request")
    approve.add_argument("approval_id")
    approve.add_argument("--by", required=True, help="name of the person approving")
    reject = sub.add_parser("reject", help="reject a request")
    reject.add_argument("approval_id")
    reject.add_argument("--by", required=True, help="name of the person rejecting")
    reject.add_argument("--reason", default="")
    args = parser.parse_args(argv)

    state_path = Path(args.state_path)
    if args.command == "list":
        print(json.dumps(pending(state_path), indent=2))
        return 0

    agent = OrchestratorAgent(
        claude=_NoModel(), state_path=state_path, audit_log_path=args.audit_log_path, mcp_bridge=mcp_bridge
    )
    if args.command == "approve":
        result = agent.execute_approved(args.approval_id, args.by)
    else:
        result = agent.reject_approval(args.approval_id, args.by, args.reason)
    print(result.content)
    return 1 if result.is_error else 0


def main() -> None:
    sys.exit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
