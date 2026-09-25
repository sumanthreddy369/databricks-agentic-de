"""Critical test: proves PHI masking is architecturally enforced, not just
claimed. Part (a) unit-tests governance_guard.enforce_masking directly. Part
(b) drives the full OrchestratorAgent with a scripted fake Claude and asserts
the raw seed `full_name`/`mrn` values never appear anywhere in `messages`.
"""

import json

from agent.llm import ToolResult
from agent.orchestrator import OrchestratorAgent
from agent.tools.governance_guard import enforce_masking

RAW_ROWS = [
    {"patient_id": "pt_00001", "mrn": "MRN-000123", "full_name": "Jane Alvarez"},
    {"patient_id": "pt_00002", "mrn": "MRN-000456", "full_name": "Robert Chen"},
]


# --- (a) unit tests -----------------------------------------------------


def test_enforce_masking_redacts_masked_columns():
    masked = enforce_masking("dim_patients", RAW_ROWS, role="clinical_reader")
    for row in masked:
        assert row["mrn"] == "***REDACTED***"
        assert row["full_name"] == "***REDACTED***"
        assert row["patient_id"] not in ("***REDACTED***",)  # unmasked columns untouched


def test_enforce_masking_leaves_phi_unmasked_role_alone():
    masked = enforce_masking("dim_patients", RAW_ROWS, role="phi_unmasked")
    assert masked == RAW_ROWS


def test_enforce_masking_does_not_mutate_input_rows():
    original = [dict(row) for row in RAW_ROWS]
    enforce_masking("dim_patients", RAW_ROWS, role="clinical_reader")
    assert RAW_ROWS == original


def test_enforce_masking_is_noop_for_unlisted_table():
    masked = enforce_masking("fct_vitals", [{"itemid": "heart_rate", "value": 88.0}], role="clinical_reader")
    assert masked == [{"itemid": "heart_rate", "value": 88.0}]


# --- (b) end-to-end via OrchestratorAgent -------------------------------


class ScriptedQueryClaude:
    """Fake Claude, same run_tool_loop signature as agent.llm.Claude. Scripts
    exactly one query_gold_table("dim_patients") tool call, then returns a
    final answer — driving dispatch()/messages the same way the real
    Anthropic tool loop would, but with zero network calls.
    """

    def run_tool_loop(self, system, messages, tools, dispatch, max_turns=6):
        messages.append(
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "query_gold_table",
                        "input": {"table": "dim_patients"},
                    }
                ],
            }
        )
        result: ToolResult = dispatch("query_gold_table", {"table": "dim_patients"})
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_1",
                        "content": result.content,
                        "is_error": result.is_error,
                    }
                ],
            }
        )
        return "Found 3 patients in dim_patients. PHI fields are masked per platform policy."


def test_query_gold_table_never_leaks_raw_phi_into_messages(tmp_path):
    duckdb_path = tmp_path / "gold.duckdb"
    orchestrator = OrchestratorAgent(
        claude=ScriptedQueryClaude(),
        duckdb_path=duckdb_path,
        seed_sql_path="data/seed/gold_seed.sql",
        audit_log_path=tmp_path / "audit_log.jsonl",
    )

    # Build the same `messages` transcript handle() would build internally,
    # but keep a local reference so we can inspect it directly afterward —
    # this is exactly the list the real Anthropic tool loop would send back
    # to the model on the next turn.
    messages = [{"role": "user", "content": "Who are the patients in dim_patients?"}]
    answer = orchestrator.claude.run_tool_loop(
        system="da-system-prompt",
        messages=messages,
        tools=[],
        dispatch=orchestrator._dispatch,
    )

    transcript_text = json.dumps(messages, default=str)
    assert "MRN-000123" not in transcript_text
    assert "Jane Alvarez" not in transcript_text
    assert "MRN-000456" not in transcript_text
    assert "Robert Chen" not in transcript_text
    assert "***REDACTED***" in transcript_text
    assert "query_gold_table" in orchestrator._tool_calls
    assert answer  # sanity: the scripted fake did return a final answer
