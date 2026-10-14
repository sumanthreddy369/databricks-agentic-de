"""Earned autonomy: approvals that retire themselves, safely.

Every remediation belongs to an action class - the tool plus what it acts on,
e.g. `quarantine_bad_records:silver_vitals:plausible_vital_value`. Each class
starts on `approval` (a person approves every execution, agent/approvals.py).

Promotion to `auto` needs both:
- evidence: the class's last PROMOTION_STREAK approval requests were all
  approved and executed successfully, with no rejection or failure in
  between (computed from the approval history in the state file); and
- a person: someone promotes it once with `python -m agent.approvals promote
  <class> --by <name>`, which is recorded.

Demotion is automatic: the first failed execution of an `auto` class puts it
back on `approval`, with the reason recorded.

Only PROMOTABLE_TOOLS can ever be promoted. Everything else - and every new
class with no history - needs a person every time. The kill switch and the
escalation ceiling still apply to `auto` executions.
"""

from datetime import UTC, datetime

PROMOTION_STREAK = 10
PROMOTABLE_TOOLS = {"quarantine_bad_records", "restart_pipeline"}


def action_class(tool: str, tool_input: dict) -> str:
    if tool == "quarantine_bad_records":
        return f"{tool}:{tool_input.get('table', '')}:{tool_input.get('expectation', '')}"
    if tool == "restart_pipeline":
        return f"{tool}:{tool_input.get('pipeline_name', '')}"
    return tool


def _classes(state: dict) -> dict:
    return state.setdefault("autonomy", {}).setdefault("classes", {})


def policy(state: dict, cls: str) -> str:
    return state.get("autonomy", {}).get("classes", {}).get(cls, {}).get("policy", "approval")


def streak(state: dict, cls: str) -> int:
    """Consecutive most-recent approval requests for `cls` that were approved
    and executed successfully. A rejection or failure ends the streak;
    pending and expired requests are skipped (no decision either way)."""
    decided = [
        a
        for a in state.get("pending_approvals", [])
        if action_class(a["tool"], a["input"]) == cls and a["status"] in {"executed", "rejected", "failed"}
    ]
    decided.sort(key=lambda a: a.get("decided_at") or "", reverse=True)
    count = 0
    for a in decided:
        if a["status"] != "executed":
            break
        count += 1
    return count


def eligibility(state: dict, cls: str) -> tuple[bool, str]:
    tool = cls.split(":", 1)[0]
    if tool not in PROMOTABLE_TOOLS:
        return False, f"{tool} can never run without a person"
    if policy(state, cls) == "auto":
        return False, "already auto"
    n = streak(state, cls)
    if n < PROMOTION_STREAK:
        return False, f"{n} of {PROMOTION_STREAK} consecutive approved, successful executions"
    return True, f"{n} consecutive approved, successful executions"


def promote(state: dict, cls: str, by: str) -> dict:
    ok, reason = eligibility(state, cls)
    if not ok:
        raise ValueError(f"{cls} is not eligible: {reason}")
    if not by or not by.strip():
        raise ValueError("promotion needs the name of the person approving it")
    record = {
        "policy": "auto",
        "promoted_by": by.strip(),
        "promoted_at": datetime.now(UTC).isoformat(),
        "evidence": reason,
    }
    _classes(state)[cls] = record
    return record


def demote(state: dict, cls: str, reason: str) -> None:
    entry = _classes(state).setdefault(cls, {})
    entry.update(policy="approval", demoted_at=datetime.now(UTC).isoformat(), demoted_because=reason)


def summary(state: dict) -> list[dict]:
    seen = {action_class(a["tool"], a["input"]) for a in state.get("pending_approvals", [])}
    seen |= set(state.get("autonomy", {}).get("classes", {}))
    rows = []
    for cls in sorted(seen):
        ok, reason = eligibility(state, cls)
        rows.append(
            {
                "class": cls,
                "policy": policy(state, cls),
                "streak": streak(state, cls),
                "eligible": ok,
                "detail": reason,
            }
        )
    return rows
