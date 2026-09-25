"""Proves the immutable-audit-trail guardrail: every dispatch() call appends
one JSON line to the append-only audit log; the file only ever grows across
calls (never rewritten/truncated); and a masked field's RAW value never
appears in any audit line — only the already-masked output does.
"""

import json
from pathlib import Path

from agent.orchestrator import OrchestratorAgent

REPO_ROOT = Path(__file__).resolve().parent.parent


class _NoopClaude:
    def run_tool_loop(self, system, messages, tools, dispatch, max_turns=6):
        return "noop"


def _read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_dispatch_appends_one_line_per_call(tmp_path):
    audit_log_path = tmp_path / "audit_log.jsonl"
    state_path = tmp_path / "pipeline_state.json"
    state_path.write_text((REPO_ROOT / "data" / "state" / "pipeline_state.example.json").read_text())

    orchestrator = OrchestratorAgent(claude=_NoopClaude(), state_path=state_path, audit_log_path=audit_log_path)

    orchestrator._dispatch("check_expectation_metrics", {})
    lines_after_1 = _read_lines(audit_log_path)
    assert len(lines_after_1) == 1

    orchestrator._dispatch("check_job_status", {})
    lines_after_2 = _read_lines(audit_log_path)
    assert len(lines_after_2) == 2
    # File only grew — the first entry is untouched.
    assert lines_after_2[0] == lines_after_1[0]

    orchestrator._dispatch("detect_schema_drift", {})
    lines_after_3 = _read_lines(audit_log_path)
    assert len(lines_after_3) == 3
    assert lines_after_3[:2] == lines_after_2


def test_audit_entries_carry_expected_fields(tmp_path):
    audit_log_path = tmp_path / "audit_log.jsonl"
    state_path = tmp_path / "pipeline_state.json"
    state_path.write_text((REPO_ROOT / "data" / "state" / "pipeline_state.example.json").read_text())

    orchestrator = OrchestratorAgent(claude=_NoopClaude(), state_path=state_path, audit_log_path=audit_log_path)
    orchestrator._current_mode = "de"
    orchestrator._dispatch("check_job_status", {})

    entry = _read_lines(audit_log_path)[0]
    assert entry["mode"] == "de"
    assert entry["tool"] == "check_job_status"
    assert entry["input"] == {}
    assert "output" in entry
    assert "ts" in entry
    assert entry["is_error"] is False


def test_masked_raw_value_never_appears_in_audit_log(tmp_path):
    audit_log_path = tmp_path / "audit_log.jsonl"
    duckdb_path = tmp_path / "gold.duckdb"

    orchestrator = OrchestratorAgent(
        claude=_NoopClaude(),
        duckdb_path=duckdb_path,
        seed_sql_path="data/seed/gold_seed.sql",
        audit_log_path=audit_log_path,
    )
    orchestrator._dispatch("query_gold_table", {"table": "dim_patients"})

    raw_log_text = audit_log_path.read_text()
    # Raw seed PHI values must never leak into the audit log — only the
    # already-masked output does.
    assert "Jane Alvarez" not in raw_log_text
    assert "MRN-000123" not in raw_log_text
    assert "Robert Chen" not in raw_log_text
    assert "***REDACTED***" in raw_log_text


def test_audit_log_is_only_ever_appended_to_at_the_filesystem_level(tmp_path):
    audit_log_path = tmp_path / "audit_log.jsonl"
    state_path = tmp_path / "pipeline_state.json"
    state_path.write_text((REPO_ROOT / "data" / "state" / "pipeline_state.example.json").read_text())

    orchestrator = OrchestratorAgent(claude=_NoopClaude(), state_path=state_path, audit_log_path=audit_log_path)

    orchestrator._dispatch("check_expectation_metrics", {})
    size_after_1 = audit_log_path.stat().st_size

    orchestrator._dispatch("check_expectation_metrics", {})
    size_after_2 = audit_log_path.stat().st_size

    assert size_after_2 > size_after_1  # strictly grew, never shrank/rewrote
