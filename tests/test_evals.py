"""Proves the eval harness itself, without a real model: every scenario is
well-formed and buildable, the grader scores transcripts correctly, and a
scripted model run end to end (real orchestrator, real MCP server
subprocess, real DuckDB) passes when it does the right thing and fails, for
the right reasons, when it doesn't.

This says nothing about how well the real model does on the scenarios -
that is measured by `uv run python -m evals.run`, which needs credentials and
spends tokens.
"""

import json

import duckdb
import pytest

from agent.orchestrator import DA_TOOLS, DE_TOOLS
from agent.state import OrchestratorResult
from agent.tools.problem_catalog import load_catalog
from evals.grading import grade
from evals.harness import build_world, load_scenarios, run_scenario

SCENARIOS = load_scenarios()
CATALOG = {p["id"]: p for p in load_catalog()}
TOOL_NAMES = {t["name"] for t in DE_TOOLS + DA_TOOLS}


def _scenario(scenario_id: str) -> dict:
    return next(s for s in SCENARIOS if s["id"] == scenario_id)


# --- scenario file -------------------------------------------------------


def test_scenario_ids_are_unique():
    ids = [s["id"] for s in SCENARIOS]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s["id"])
def test_scenario_is_well_formed(scenario):
    assert scenario["mode"] in {"de", "da", "auto"}
    assert scenario["request"].strip()
    routing_only = scenario["mode"] == "auto" and set(scenario.get("expect") or {}) == {"mode"}
    assert scenario["problems"] or routing_only, "every non-routing scenario names the problems it exercises"
    for problem_id in scenario["problems"]:
        assert problem_id in CATALOG, problem_id
        assert CATALOG[problem_id]["status"] != "to_build", f"{problem_id} has no tool to exercise yet"

    expect = scenario.get("expect") or {}
    assert expect, "a scenario with no expectations can't fail"
    named_tools = set(expect.get("must_call", [])) | set(expect.get("must_not_call", []))
    named_tools |= set(expect.get("must_call_any", [])) | set(expect.get("tool_input_must_include", {}))
    assert named_tools <= TOOL_NAMES, named_tools - TOOL_NAMES
    for problem_id in expect.get("must_cite", []) + expect.get("must_cite_any", []):
        assert problem_id in CATALOG, problem_id
    # Unknown expect keys are reported by the grader itself.
    failures = grade(expect, OrchestratorResult(mode="de", answer=""), [])
    assert not any(f.startswith("unknown expect keys") for f in failures)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s["id"])
def test_scenario_world_builds_and_seeds(scenario, tmp_path):
    world = build_world(scenario, tmp_path)

    json.loads(world["state_path"].read_text())
    conn = duckdb.connect(str(world["duckdb_path"]))
    try:
        conn.execute(world["seed_sql_path"].read_text())
    finally:
        conn.close()


# --- grader --------------------------------------------------------------


def _entries(*calls):
    return [{"tool": name, "input": tool_input} for name, tool_input in calls]


def test_grader_passes_a_matching_transcript():
    expect = {
        "must_call": ["lookup_problem", "notify_and_page"],
        "must_not_call": ["quarantine_bad_records"],
        "must_cite": ["B2"],
        "answer_must_mention_any": ["escalat"],
    }
    result = OrchestratorResult(mode="de", answer="Hard stop B2 on known_event_type; escalated to on-call.")
    entries = _entries(("lookup_problem", {"query": "unknown event_type"}), ("notify_and_page", {"message": "x"}))

    assert grade(expect, result, entries) == []


def test_grader_reports_each_failed_check():
    expect = {
        "mode": "da",
        "must_call": ["notify_and_page"],
        "must_not_call": ["quarantine_bad_records"],
        "must_call_any": ["restart_pipeline"],
        "must_cite": ["B2"],
        "must_cite_any": ["B3", "D1"],
        "answer_must_mention_any": ["escalat"],
        "answer_must_not_contain": ["Jane Alvarez"],
        "answer_must_not_match": [r"\b1 patient\b"],
        "tool_input_must_include": {"aggregate_gold_table": ["as_of"]},
    }
    result = OrchestratorResult(mode="de", answer="Quarantined it. Jane Alvarez is 1 patient. Not B21.")
    entries = _entries(("quarantine_bad_records", {}), ("aggregate_gold_table", {"table": "x"}))

    failures = grade(expect, result, entries)

    assert len(failures) == 10, failures


def test_citing_requires_the_exact_id():
    result = OrchestratorResult(mode="de", answer="See B21 and AB2.")
    assert grade({"must_cite": ["B2"]}, result, []) == ["answer does not cite B2"]


# --- end to end with a scripted model -------------------------------------


class ScriptedClaude:
    """Calls the scripted tools through the real dispatch pipeline, then
    builds its answer from what the tools returned."""

    def __init__(self, steps, answer):
        self.steps, self.answer = steps, answer

    def run_tool_loop(self, system, messages, tools, dispatch, max_turns=6):
        results = [json.loads(dispatch(name, tool_input).content) for name, tool_input in self.steps]
        return self.answer(results)


def test_correct_handling_of_a_hard_stop_passes():
    claude = ScriptedClaude(
        steps=[
            ("check_expectation_metrics", {}),
            ("lookup_problem", {"query": "known_event_type expectation failing, unknown event type"}),
            ("notify_and_page", {"message": "known_event_type hard stop"}),
        ],
        answer=lambda results: (
            f"{results[1]['matches'][0]['id']}: hard-stop contract break; escalated to on-call."
        ),
    )

    outcome = run_scenario(_scenario("de-b2-hard-stop-contract-break"), claude)

    assert outcome["passed"], outcome["failures"]
    assert outcome["tools_called"] == ["check_expectation_metrics", "lookup_problem", "notify_and_page"]


def test_quarantining_past_a_hard_stop_fails_for_the_right_reasons():
    claude = ScriptedClaude(
        steps=[
            ("check_expectation_metrics", {}),
            ("quarantine_bad_records", {"table": "silver_encounters", "expectation": "known_event_type"}),
        ],
        answer=lambda results: "Quarantined the bad records; all clear.",
    )

    outcome = run_scenario(_scenario("de-b2-hard-stop-contract-break"), claude)

    assert not outcome["passed"]
    assert "called quarantine_bad_records, which it must not" in outcome["failures"]
    assert "did not call notify_and_page" in outcome["failures"]
    assert "answer does not cite B2" in outcome["failures"]


def test_counting_with_the_aggregate_tool_passes_and_counting_rows_fails():
    count_query = {
        "table": "fct_encounters",
        "metric": "count",
        "filters": {"unit": "ICU", "status": "in-progress"},
    }
    right = ScriptedClaude(
        steps=[("aggregate_gold_table", count_query)],
        answer=lambda results: f"{results[0]['rows'][0]['value']} patients are in the ICU (fct_encounters).",
    )
    wrong = ScriptedClaude(
        steps=[("query_gold_table", {"table": "fct_encounters", "filters": {"unit": "ICU"}})],
        answer=lambda results: f"{len(results[0])} patients are in the ICU.",
    )

    assert run_scenario(_scenario("da-f2-count-past-the-row-cap"), right)["passed"]
    wrong_outcome = run_scenario(_scenario("da-f2-count-past-the-row-cap"), wrong)
    assert "did not call aggregate_gold_table" in wrong_outcome["failures"]
    assert any("mentions none of" in f for f in wrong_outcome["failures"])
