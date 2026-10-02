"""The agent's knowledge of real-time problems: lookup_problem.

agent/knowledge/problem_catalog.yaml lists the real-time problems a data
engineer and analyst hit on this Databricks stack (ingestion, DLT, Unity
Catalog, overload, outages, timestamps, data content, getting changes to
production, analyst SQL pitfalls), each with how it shows up, the expected
response, and an autonomy level. This tool lets the agent match what it just
observed ("no events for 20 minutes but every job is green") against that
catalog, so it can name the problem, cite its ID, and follow the response the
catalog prescribes instead of improvising one.

Matching is deterministic keyword scoring over each problem's title,
description, and detection text, with a small synonym table for the words
people actually use during incidents. No embedding model, no network: the
same function runs in tests, in the MCP server, and on a job cluster. When
Databricks Vector Search is live (agent/tools/knowledge_search.py) this can
move to semantic search; until then a miss returns an explicit "new problem,
escalate" answer rather than a weak guess.

The autonomy level is guidance to the model, attached to every match. What
is enforced in code is unchanged: the kill switch and escalation ceiling in
agent/orchestrator.py, and the masking/injection/size guardrails on every
tool result.
"""

import json
import re
from functools import lru_cache
from pathlib import Path

import yaml

from agent.llm import ToolResult

CATALOG_PATH = Path(__file__).resolve().parent.parent / "knowledge" / "problem_catalog.yaml"

DEFAULT_LIMIT = 3

LEVEL_GUIDANCE = {
    "L0": "Observe and report only. Take no action.",
    "L1": "Diagnose and recommend. Escalate via notify_and_page when a human must act. "
    "Do not call quarantine_bad_records or restart_pipeline for this problem.",
    "L2": "You may remediate with quarantine_bad_records / restart_pipeline; the kill switch and escalation "
    "ceiling still apply. Escalate if the remediation doesn't clear it.",
    "L3": "Enforced in code on every request. Explain what the guardrail did; never try to work around it.",
}

_STOPWORDS = {
    "the", "and", "for", "but", "not", "are", "was", "were", "with", "from", "that", "this", "has", "have",
    "into", "its", "our", "out", "all", "any", "can", "why", "how", "what", "when", "who", "does", "did",
    "get", "got", "one", "too", "very", "just", "still", "than", "then", "there", "they", "them", "been",
    "being", "will", "would", "should", "could", "about", "after", "before", "while", "every", "each",
}  # fmt: skip

# Incident vocabulary -> catalog vocabulary. Applied to the query only.
_SYNONYMS = {
    "lag": ["backlog", "behind"],
    "lagging": ["backlog", "behind"],
    "slow": ["behind", "duration"],
    "down": ["outage", "fails", "unavailable"],
    "outage": ["down", "unavailable"],
    "crash": ["fails", "failed"],
    "crashed": ["fails", "failed"],
    "zero": ["0", "sentinel"],
    "0": ["sentinel", "zero"],
    "silent": ["silence", "stops", "sending"],
    "quiet": ["silence", "stops", "sending"],
    "green": ["healthy", "silence"],
    "nothing": ["silence", "stops"],
    "flood": ["spike", "floods", "volume"],
    "overload": ["spike", "volume", "backlog"],
    "spike": ["volume", "10x"],
    "timezone": ["time", "zone", "offset"],
    "clock": ["skew", "time"],
    "1970": ["clock", "skew", "reset"],
    "fahrenheit": ["unit", "temp", "celsius"],
    "duplicate": ["duplicates", "dedup", "replay"],
    "duplicates": ["dedup", "replay"],
    "transfer": ["transfers", "unit", "transferred"],
    "transferred": ["transfer", "unit"],
    "wrong": ["mismatch", "misattribute"],
    "phi": ["masked", "mask", "pii"],
    "name": ["masked", "phi"],
    "count": ["counts", "aggregates", "counting"],
    "500": ["cap", "capped", "rows"],
    "deploy": ["deployment", "release"],
    "deployed": ["deployment", "deploy"],
    "rollback": ["previous", "version", "deploy"],
    "permission": ["permission_denied", "grant", "access"],
    "denied": ["permission", "grant"],
    "403": ["permission", "access"],
    "oom": ["memory", "infrastructure"],
    "memory": ["oom"],
    "quota": ["capacity", "gcp"],
    "cost": ["dbu", "budget", "spend"],
    "stale": ["freshness", "fresh", "old"],
    "late": ["watermark", "delay", "late"],
    "unknown": ["contract", "hard-stop", "unrecognized"],
}


@lru_cache(maxsize=1)
def load_catalog() -> list[dict]:
    """All problems as flat dicts, each with its layer code and title added."""
    data = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))
    problems = []
    for layer in data["layers"]:
        for problem in layer["problems"]:
            problems.append({**problem, "layer": layer["code"], "layer_title": layer["title"]})
    return problems


def _stem(token: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if len(token) > len(suffix) + 3 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def _tokens(text: str) -> list[str]:
    return [_stem(t) for t in re.findall(r"[a-z0-9_]+", text.lower()) if len(t) >= 2 and t not in _STOPWORDS]


@lru_cache(maxsize=1)
def _index() -> list[tuple[dict, set[str], set[str], set[str]]]:
    return [
        (p, set(_tokens(p["title"])), set(_tokens(p.get("description", ""))), set(_tokens(p.get("detect", ""))))
        for p in load_catalog()
    ]


def _query_terms(query: str) -> set[str]:
    raw = re.findall(r"[a-z0-9_]+", query.lower())
    terms = set(_tokens(query))
    for word in raw:
        for synonym in _SYNONYMS.get(word, []):
            terms.update(_tokens(synonym))
    return terms


def _guidance(level: str) -> str:
    return " / ".join(LEVEL_GUIDANCE[part] for part in re.findall(r"L[0-3]", level))


def _render(problem: dict) -> dict:
    return {
        "id": problem["id"],
        "layer": f"{problem['layer']}. {problem['layer_title']}",
        "title": problem["title"],
        "description": problem.get("description", ""),
        "how_it_shows_up": problem.get("detect", ""),
        "expected_response": problem.get("action", ""),
        "autonomy_level": problem["level"],
        "what_you_may_do": _guidance(problem["level"]),
        "repo_status": problem["status"] + (f" - {problem['status_note']}" if problem.get("status_note") else ""),
    }


def search(query: str, limit: int = DEFAULT_LIMIT) -> list[dict]:
    """Ranked catalog problems for a free-text description of what was
    observed. Title matches weigh most, then description, then detection."""
    terms = _query_terms(query)
    scored = []
    for problem, title, description, detect in _index():
        score = 3 * len(terms & title) + 2 * len(terms & description) + len(terms & detect)
        if score:
            scored.append((score, problem["id"], problem))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [problem for _, _, problem in scored[:limit]]


def lookup_problem(
    query: str | None = None, problem_id: str | None = None, limit: int = DEFAULT_LIMIT
) -> ToolResult:
    """Looks up catalog problems by ID or by a description of what was
    observed. Returns {"matches": [...]} with the expected response and the
    autonomy level for each, or an explicit "no match" answer."""
    if problem_id:
        wanted = problem_id.strip().upper()
        match = next((p for p in load_catalog() if p["id"] == wanted), None)
        if match is None:
            return ToolResult(
                tool_use_id="", content=json.dumps({"error": f"unknown problem id: {problem_id}"}), is_error=True
            )
        return ToolResult(tool_use_id="", content=json.dumps({"matches": [_render(match)]}))

    if not query or not query.strip():
        return ToolResult(
            tool_use_id="", content=json.dumps({"error": "give a query or a problem_id"}), is_error=True
        )

    matches = search(query, limit=max(1, min(limit, 5)))
    if not matches:
        payload = {
            "matches": [],
            "message": "No catalog match. Treat it as a new problem: report exactly what you observed and "
            "escalate via notify_and_page rather than remediating.",
        }
    else:
        payload = {"matches": [_render(p) for p in matches]}
    return ToolResult(tool_use_id="", content=json.dumps(payload))
