"""Deterministic grading for evals/scenarios.yaml.

Grades what the agent did, not how it phrased it: the tools it called (from
the orchestrator's audit log, which records every dispatch and its input),
the routed mode, problem IDs cited, and plain substring/regex checks on the
final answer. No LLM judge, so a grade never depends on another model's
opinion and the same transcript always grades the same way.
"""

import re

from agent.state import OrchestratorResult

_EXPECT_KEYS = {
    "mode",
    "must_call",
    "must_not_call",
    "must_call_any",
    "must_cite",
    "must_cite_any",
    "answer_must_mention_any",
    "answer_must_not_contain",
    "answer_must_not_match",
    "tool_input_must_include",
}


def _cites(answer: str, problem_id: str) -> bool:
    return re.search(rf"\b{re.escape(problem_id)}\b", answer) is not None


def grade(expect: dict, result: OrchestratorResult, audit_entries: list[dict]) -> list[str]:
    """Returns the list of failed checks; empty means the scenario passed."""
    unknown = set(expect) - _EXPECT_KEYS
    if unknown:
        return [f"unknown expect keys: {sorted(unknown)}"]

    called = [entry["tool"] for entry in audit_entries]
    answer = result.answer or ""
    lowered = answer.lower()
    failures = []

    if "mode" in expect and result.mode != expect["mode"]:
        failures.append(f"routed to {result.mode}, expected {expect['mode']}")
    for tool in expect.get("must_call", []):
        if tool not in called:
            failures.append(f"did not call {tool}")
    for tool in expect.get("must_not_call", []):
        if tool in called:
            failures.append(f"called {tool}, which it must not")
    if expect.get("must_call_any") and not set(expect["must_call_any"]) & set(called):
        failures.append(f"called none of {expect['must_call_any']}")
    for problem_id in expect.get("must_cite", []):
        if not _cites(answer, problem_id):
            failures.append(f"answer does not cite {problem_id}")
    if expect.get("must_cite_any") and not any(_cites(answer, pid) for pid in expect["must_cite_any"]):
        failures.append(f"answer cites none of {expect['must_cite_any']}")
    if expect.get("answer_must_mention_any") and not any(
        text.lower() in lowered for text in expect["answer_must_mention_any"]
    ):
        failures.append(f"answer mentions none of {expect['answer_must_mention_any']}")
    for text in expect.get("answer_must_not_contain", []):
        if text.lower() in lowered:
            failures.append(f"answer contains {text!r}")
    for pattern in expect.get("answer_must_not_match", []):
        if re.search(pattern, answer, re.IGNORECASE):
            failures.append(f"answer matches {pattern!r}")
    for tool, keys in expect.get("tool_input_must_include", {}).items():
        inputs = [entry.get("input") or {} for entry in audit_entries if entry["tool"] == tool]
        for key in keys:
            if not any(key in tool_input for tool_input in inputs):
                failures.append(f"no {tool} call used {key}")
    return failures
