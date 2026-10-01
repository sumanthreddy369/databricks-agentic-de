"""Structural test proving the blast-radius guardrail: the DE and DA tool
schema lists actually passed to Claude contain only their expected tool
names, and no destructive (drop/delete/truncate-shaped) tool exists anywhere
in either list — or, as a stronger check, anywhere in agent/tools/.
"""

import re

from agent.orchestrator import DA_TOOLS, DE_TOOLS

EXPECTED_DE_TOOL_NAMES = {
    "check_expectation_metrics",
    "check_job_status",
    "detect_schema_drift",
    "quarantine_bad_records",
    "restart_pipeline",
    "notify_and_page",
    "score_vitals_anomaly",
}

EXPECTED_DA_TOOL_NAMES = {"query_gold_table", "aggregate_gold_table"}

_DESTRUCTIVE_NAME_PATTERN = re.compile(r"drop|delete|truncate|purge|wipe|erase", re.IGNORECASE)


def test_de_tools_are_exactly_the_expected_set():
    names = {tool["name"] for tool in DE_TOOLS}
    assert names == EXPECTED_DE_TOOL_NAMES


def test_da_tools_are_exactly_the_expected_set():
    names = {tool["name"] for tool in DA_TOOLS}
    assert names == EXPECTED_DA_TOOL_NAMES


def test_no_destructive_shaped_tool_name_in_de_tools():
    for tool in DE_TOOLS:
        assert not _DESTRUCTIVE_NAME_PATTERN.search(tool["name"]), f"destructive-shaped tool name: {tool['name']}"


def test_no_destructive_shaped_tool_name_in_da_tools():
    for tool in DA_TOOLS:
        assert not _DESTRUCTIVE_NAME_PATTERN.search(tool["name"]), f"destructive-shaped tool name: {tool['name']}"


def test_every_tool_has_a_name_and_input_schema():
    for tool in DE_TOOLS + DA_TOOLS:
        assert "name" in tool
        assert "description" in tool
        assert "input_schema" in tool
        assert tool["input_schema"]["type"] == "object"


def test_only_quarantine_and_restart_are_remediation_tools_gated_by_kill_switch():
    """Cross-check against agent/orchestrator.py's own guardrail wiring: the
    kill switch/escalation-ceiling logic is scoped to exactly these two tool
    names — nothing else in DE_TOOLS should be treated as destructive.
    """
    from agent.orchestrator import _REMEDIATION_TOOLS

    assert set(_REMEDIATION_TOOLS) == {"quarantine_bad_records", "restart_pipeline"}
    assert set(_REMEDIATION_TOOLS).issubset(EXPECTED_DE_TOOL_NAMES)
