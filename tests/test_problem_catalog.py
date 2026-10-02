"""Proves the agent's problem knowledge is well-formed and findable:
agent/knowledge/problem_catalog.yaml is internally consistent, and
lookup_problem maps realistic incident descriptions to the right problem and
attaches the right autonomy guidance. Zero network calls.

The matching cases below are hand-written, so they show the matcher works on
the phrasing it was built for; they are not an independent benchmark. How
well the real model uses the catalog is measured by evals/ (see
evals/README.md), not here.
"""

import json
import re

import pytest

from agent.llm import ToolResult
from agent.orchestrator import DA_TOOLS, DE_TOOLS, OrchestratorAgent
from agent.tools.problem_catalog import LEVEL_GUIDANCE, load_catalog, lookup_problem, search

VALID_STATUS = {"exists", "partial", "to_build"}
VALID_WHO = {"DE", "DA", "DE + DA"}


def test_catalog_entries_are_complete_and_unique():
    problems = load_catalog()
    ids = [p["id"] for p in problems]
    assert len(ids) == len(set(ids))
    assert len(problems) >= 100
    for p in problems:
        assert re.fullmatch(r"[A-Z]\d{1,2}", p["id"]), p["id"]
        assert p["id"].startswith(p["layer"]), p["id"]
        for field in ("title", "detect", "action", "level", "status"):
            assert p.get(field), f"{p['id']} missing {field}"
        assert p["status"] in VALID_STATUS, p["id"]
        assert p["who"] in VALID_WHO, p["id"]
        assert re.fullmatch(r"L[0-3](-L[0-3])?", p["level"]), p["id"]


@pytest.mark.parametrize(
    ("observed", "expected"),
    [
        ("no events arriving for 20 minutes but every job is green", "K2"),
        ("heart rate readings of 0 from a disconnected sensor", "M4"),
        ("vitals counted under the wrong unit after a patient transfer", "L6"),
        ("how many patients are in the ICU, the count looks capped at 500", "F2"),
        ("pipeline failed on an unknown event_type contract break", "B2"),
        ("kafka consumer lag keeps growing, backlog behind the topic", "A1"),
        ("device timestamps show 1970 after battery swap", "L1"),
        ("kafka broker is down", "K1"),
        ("traffic spike 10x normal volume mass casualty", "J1"),
        ("plausible_vital_value expectation dropping many rows suddenly", "B1"),
        ("expectation failures started right after we deployed the new release", "N3"),
        ("update fails after deploy with incompatible state checkpoint error", "N2"),
        ("autoloader gets 403 from the GCS bucket", "I1"),
        ("pipeline update failed out of memory OOM", "B3"),
        ("source sends local time without timezone offset", "L3"),
        ("sql warehouse is stopped, first query times out", "G1"),
        ("our monitoring calls to the databricks api are failing, cannot see anything", "K5"),
    ],
)
def test_incident_descriptions_find_the_right_problem(observed, expected):
    assert expected in [p["id"] for p in search(observed)]


def test_lookup_by_id_carries_the_autonomy_guidance():
    payload = json.loads(lookup_problem(problem_id="b2").content)

    [match] = payload["matches"]
    assert match["id"] == "B2"
    assert match["autonomy_level"] == "L1"
    assert match["what_you_may_do"] == LEVEL_GUIDANCE["L1"]
    assert "Do not call quarantine_bad_records or restart_pipeline" in match["what_you_may_do"]


def test_range_levels_combine_both_guidances():
    match = json.loads(lookup_problem(problem_id="A1").content)["matches"][0]

    assert match["autonomy_level"] == "L1-L2"
    assert LEVEL_GUIDANCE["L1"] in match["what_you_may_do"]
    assert LEVEL_GUIDANCE["L2"] in match["what_you_may_do"]


def test_no_match_says_treat_as_new_and_escalate():
    payload = json.loads(lookup_problem(query="xylophone quasar").content)

    assert payload["matches"] == []
    assert "escalate" in payload["message"]


@pytest.mark.parametrize("kwargs", [{}, {"query": "   "}, {"problem_id": "Z99"}])
def test_bad_lookups_are_tool_errors(kwargs):
    assert lookup_problem(**kwargs).is_error is True


def test_lookup_is_offered_in_both_modes():
    assert "lookup_problem" in {t["name"] for t in DE_TOOLS}
    assert "lookup_problem" in {t["name"] for t in DA_TOOLS}


class _NoopClaude:
    def run_tool_loop(self, system, messages, tools, dispatch, max_turns=6):
        return "noop"


def test_orchestrator_routes_lookup_through_the_mcp_bridge(tmp_path):
    calls = []

    class _RecordingBridge:
        def dispatch(self, tool_name, tool_input):
            calls.append((tool_name, tool_input))
            return lookup_problem(**tool_input)

    orchestrator = OrchestratorAgent(
        claude=_NoopClaude(), audit_log_path=tmp_path / "audit.jsonl", mcp_bridge=_RecordingBridge()
    )
    result: ToolResult = orchestrator._dispatch("lookup_problem", {"query": "kafka broker is down"})

    assert calls == [("lookup_problem", {"query": "kafka broker is down"})]
    assert json.loads(result.content)["matches"][0]["id"] == "K1"
