"""Human-work report: how much of the data work needed a person.

The plan (docs/plan.md) says "less human work" has to be measured, not
claimed. This report is that measurement, built only from what the system
already records:

- the append-only audit log (one line per tool call, grouped by request_id);
- the approval requests in the pipeline state file.

    uv run python -m agent.human_work                 # last 7 days
    uv run python -m agent.human_work --days 30 --json

It reports, for the window:
- requests by mode (analyst questions, pipeline health checks);
- how many needed a person: an escalation (notify_and_page) or a fix
  waiting for approval;
- approval outcomes and how long each human decision took;
- which catalog problems came up (from lookup_problem);
- safety events: identifier requests refused, results withheld, small groups
  suppressed, untrusted text wrapped.

It counts what was recorded; it can't know about work people did outside
the system, so "share handled without a person" is an upper bound until
every request path goes through this layer.
"""

import argparse
import json
import os
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median

from agent.orchestrator import DEFAULT_AUDIT_LOG_PATH

DEFAULT_STATE_PATH = "data/state/pipeline_state.json"
ANALYST_TOOLS = {"ask_genie", "compute_metric", "aggregate_gold_table", "query_gold_table"}


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _json(text: str):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def build(audit_lines: list[dict], approvals: list[dict], since: datetime, until: datetime) -> dict:
    entries = [e for e in audit_lines if since <= _ts(e["ts"]) < until]
    by_request: dict[str, list[dict]] = {}
    for e in entries:
        if e.get("mode") == "approval":
            continue
        by_request.setdefault(e.get("request_id") or f"untracked_{e['ts']}", []).append(e)

    modes = Counter()
    needed_person, escalated, fix_waiting = 0, 0, 0
    answer_sources = Counter()
    problems = Counter()
    safety = Counter()
    for calls in by_request.values():
        mode = calls[0].get("mode", "unknown")
        modes[mode] += 1
        tools = [c["tool"] for c in calls]
        outputs = [_json(c.get("output")) or {} for c in calls]
        was_escalated = "notify_and_page" in tools
        had_pending = any(isinstance(o, dict) and o.get("pending_approval") for o in outputs)
        escalated += was_escalated
        fix_waiting += had_pending
        needed_person += was_escalated or had_pending
        if mode == "da":
            used = [t for t in tools if t in ANALYST_TOOLS]
            answer_sources[used[0] if used else "no data tool"] += 1
        for c, o in zip(calls, outputs, strict=True):
            if c["tool"] == "lookup_problem" and isinstance(o, dict) and o.get("matches"):
                problems[o["matches"][0]["id"]] += 1
            if isinstance(o, dict):
                if o.get("refused"):
                    safety["identifier requests refused"] += 1
                if o.get("withheld"):
                    safety["results withheld (SQL outside Gold)"] += 1
                if o.get("suppressed_groups") or (o.get("guardrails") or {}).get("suppressed_cells"):
                    safety["answers with small groups suppressed"] += 1
            if str(c.get("output", "")).startswith("<untrusted_data>"):
                safety["untrusted text wrapped"] += 1

    window_approvals = [a for a in approvals if since <= _ts(a["requested_at"]) < until]
    outcomes = Counter(a["status"] for a in window_approvals)
    decision_minutes = [
        (_ts(a["decided_at"]) - _ts(a["requested_at"])).total_seconds() / 60
        for a in window_approvals
        if a.get("decided_at") and a["status"] in {"executed", "failed", "rejected"}
    ]
    total = sum(modes.values())
    return {
        "window": {"since": since.isoformat(), "until": until.isoformat()},
        "requests": {"total": total, "by_mode": dict(modes)},
        "needed_a_person": {
            "requests": needed_person,
            "escalations": escalated,
            "fixes_waiting_for_approval": fix_waiting,
            "share_handled_without_a_person": round(1 - needed_person / total, 3) if total else None,
        },
        "analyst_answers_by_source": dict(answer_sources),
        "approvals": {
            "requested": len(window_approvals),
            "by_outcome": dict(outcomes),
            "approvers": dict(Counter(a.get("decided_by") for a in window_approvals if a.get("decided_by"))),
            "median_decision_minutes": round(median(decision_minutes), 1) if decision_minutes else None,
        },
        "problems_by_catalog_id": dict(problems.most_common()),
        "safety_events": dict(safety),
    }


def to_markdown(report: dict) -> str:
    r, n, a = report["requests"], report["needed_a_person"], report["approvals"]
    share = n["share_handled_without_a_person"]
    lines = [
        f"# Human-work report ({report['window']['since'][:10]} to {report['window']['until'][:10]})",
        "",
        f"- Requests: **{r['total']}** ({', '.join(f'{k}: {v}' for k, v in r['by_mode'].items()) or 'none'})",
        f"- Needed a person: **{n['requests']}** (escalations {n['escalations']}, fixes waiting for approval "
        f"{n['fixes_waiting_for_approval']})",
        f"- Handled without a person: **{share:.0%}**" if share is not None else "- Handled without a person: n/a",
        f"- Approvals: {a['requested']} requested, outcomes {a['by_outcome'] or 'none'}, median decision "
        f"{a['median_decision_minutes'] if a['median_decision_minutes'] is not None else 'n/a'} min",
        f"- Analyst answers by source: {report['analyst_answers_by_source'] or 'none'}",
        f"- Problems seen (catalog IDs): {report['problems_by_catalog_id'] or 'none'}",
        f"- Safety events: {report['safety_events'] or 'none'}",
        "",
        "Counts only what passed through this layer; work done outside it isn't visible here.",
    ]
    return "\n".join(lines)


def _read_audit(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = _json(line)
        if isinstance(entry, dict) and "ts" in entry and "tool" in entry:
            out.append(entry)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent.human_work", description=__doc__.split("\n\n")[0])
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--audit-log-path", default=os.environ.get("AUDIT_LOG_PATH", DEFAULT_AUDIT_LOG_PATH))
    parser.add_argument("--state-path", default=os.environ.get("PIPELINE_STATE_PATH", DEFAULT_STATE_PATH))
    parser.add_argument("--json", action="store_true", help="print JSON instead of markdown")
    args = parser.parse_args(argv)

    # The window ends now, inclusive. build() is half-open, and on Windows an
    # entry written just before this call can carry the identical timestamp
    # (coarse clock), which made tests/test_human_work_report.py flaky.
    until = datetime.now(UTC) + timedelta(microseconds=1)
    since = until - timedelta(days=args.days)
    state_path = Path(args.state_path)
    approvals = (
        (_json(state_path.read_text(encoding="utf-8")) or {}).get("pending_approvals", [])
        if state_path.exists()
        else []
    )
    report = build(_read_audit(Path(args.audit_log_path)), approvals, since, until)
    print(json.dumps(report, indent=2) if args.json else to_markdown(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
