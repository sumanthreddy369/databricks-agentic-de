"""Proves the groundedness guardrail: a zero-row query_gold_table result is
short-circuited to a fixed, deterministic refusal string INSTEAD OF an empty
JSON list flowing to the model as if it were ordinary (if unremarkable) data
— which would invite the model to speculate about why there's nothing there.
"""

import json

from agent.orchestrator import GROUNDEDNESS_REFUSAL, OrchestratorAgent


class _NoopClaude:
    def run_tool_loop(self, system, messages, tools, dispatch, max_turns=6):
        return "noop"


def test_zero_row_query_returns_fixed_refusal_not_empty_list(tmp_path):
    duckdb_path = tmp_path / "gold.duckdb"
    orchestrator = OrchestratorAgent(
        claude=_NoopClaude(),
        duckdb_path=duckdb_path,
        seed_sql_path="data/seed/gold_seed.sql",
        audit_log_path=tmp_path / "audit_log.jsonl",
    )

    # patient_id that does not exist in the seed data -> zero rows.
    result = orchestrator._dispatch(
        "query_gold_table", {"table": "dim_patients", "filters": {"patient_id": "pt_does_not_exist"}}
    )

    assert result.content == GROUNDEDNESS_REFUSAL
    assert result.is_error is False
    # Critically NOT an empty JSON list — that would look like normal,
    # unremarkable queryable data rather than a refusal to speculate.
    assert result.content != "[]"
    try:
        json.loads(result.content)
        parsed_as_json = True
    except json.JSONDecodeError:
        parsed_as_json = False
    assert parsed_as_json is False  # it's a plain refusal sentence, not a data payload


def test_non_empty_query_returns_normal_masked_rows(tmp_path):
    duckdb_path = tmp_path / "gold.duckdb"
    orchestrator = OrchestratorAgent(
        claude=_NoopClaude(),
        duckdb_path=duckdb_path,
        seed_sql_path="data/seed/gold_seed.sql",
        audit_log_path=tmp_path / "audit_log.jsonl",
    )

    result = orchestrator._dispatch("query_gold_table", {"table": "dim_patients"})

    assert result.content != GROUNDEDNESS_REFUSAL
    rows = json.loads(result.content)
    assert len(rows) == 3
    assert rows[0]["mrn"] == "***REDACTED***"
