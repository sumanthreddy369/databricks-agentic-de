"""Deterministic planner for the DE health check: what runs when no model is
available or wanted (no Anthropic key, an outage, or a team that wants the
check fully predictable).

It stands in for agent.llm.Claude in OrchestratorAgent (same run_tool_loop
signature), so every call still goes through the orchestrator's dispatch:
masking, the injection scan, the approval gate, the kill switch, the
escalation ceiling and the audit log all apply unchanged. The plan:

1. check_expectation_metrics and check_job_status;
2. lookup_problem for each failing expectation and each unhealthy job;
3. act on the catalog's autonomy level, never beyond it:
   - L0: observe and report only;
   - L1, or no catalog match (a new problem): one notify_and_page for all of them;
   - L2: request quarantine_bad_records - which waits for a person's
     approval unless that fix class has earned autonomy.

The model, when present, can reason about context the rules can't (for
example whether a drop spike's cause is bounded). The rules never do more
than the catalog allows, so they are the safe floor.
"""

import json


def _load(result) -> dict:
    try:
        payload = json.loads(result.content)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _short(name: str) -> str:
    return name.split(".")[-1]


class RulesPlanner:
    def run_tool_loop(self, system, messages, tools, dispatch, max_turns=6) -> str:
        expectations = _load(dispatch("check_expectation_metrics", {}))
        jobs = _load(dispatch("check_job_status", {}))

        findings = [
            {
                "what": f"{f['expectation']} failing in {_short(f['table'])}",
                "table": _short(f["table"]),
                "expectation": f["expectation"],
                "count": f.get("failure_count", 0),
            }
            for f in expectations.get("failing_expectations", [])
        ]
        findings += [
            {"what": f"pipeline job {name} is {status}", "table": None, "expectation": None, "count": None}
            for name, status in (jobs.get("unhealthy") or {}).items()
        ]
        if expectations.get("error") or jobs.get("error"):
            findings.append(
                {
                    "what": "health check could not read pipeline state: "
                    + str(expectations.get("error") or jobs.get("error")),
                    "table": None,
                    "expectation": None,
                    "count": None,
                }
            )

        page, requested, observed = [], [], []
        for f in findings:
            match = (_load(dispatch("lookup_problem", {"query": f["what"]})).get("matches") or [None])[0]
            level = (match or {}).get("autonomy_level", "")
            label = f"{f['what']}" + (f" ({f['count']:,} records)" if f["count"] else "")
            label += (
                f" -> {match['id']} {match['title']} [{level}]" if match else " -> no catalog match (new problem)"
            )
            if match and level.startswith("L0"):
                observed.append(label)
            elif match and "L2" in level and f["table"] and f["expectation"]:
                dispatch("quarantine_bad_records", {"table": f["table"], "expectation": f["expectation"]})
                requested.append(label)
            else:
                page.append(label)

        if page:
            dispatch("notify_and_page", {"message": "Health check needs a person: " + "; ".join(page)})

        if not findings:
            return "Healthy: no failing expectations, all pipelines healthy."
        lines = [f"{len(findings)} finding(s)."]
        if page:
            lines.append("Escalated to a person: " + "; ".join(page))
        if requested:
            lines.append("Fix requested (waits for approval unless earned): " + "; ".join(requested))
        if observed:
            lines.append("Observed only: " + "; ".join(observed))
        return " ".join(lines)
