"""Proves detect_anomalous_activity is a pure function over the audit log:
it returns True when notify_and_page calls within the trailing window exceed
the threshold, and False otherwise — using a synthetic audit log with fully
controllable timestamps (via the injectable `now` parameter) rather than the
real wall clock, so this is deterministic.
"""

import json
from datetime import UTC, datetime, timedelta

from agent.orchestrator import detect_anomalous_activity

REFERENCE_NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)


def _write_audit_log(path, entries: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def _notify_entry(ts: datetime) -> dict:
    return {"ts": ts.isoformat(), "mode": "de", "tool": "notify_and_page", "input": {}, "output": "{}"}


def _other_entry(ts: datetime, tool: str = "check_job_status") -> dict:
    return {"ts": ts.isoformat(), "mode": "de", "tool": tool, "input": {}, "output": "{}"}


def test_returns_false_when_log_file_does_not_exist(tmp_path):
    missing_path = tmp_path / "does_not_exist.jsonl"
    assert detect_anomalous_activity(missing_path, now=REFERENCE_NOW) is False


def test_returns_false_below_threshold(tmp_path):
    log_path = tmp_path / "audit_log.jsonl"
    entries = [_notify_entry(REFERENCE_NOW - timedelta(minutes=m)) for m in (5, 10, 15)]
    _write_audit_log(log_path, entries)

    assert (
        detect_anomalous_activity(log_path, window_minutes=60, escalation_threshold=5, now=REFERENCE_NOW)
        is False
    )


def test_returns_true_above_threshold_within_window(tmp_path):
    log_path = tmp_path / "audit_log.jsonl"
    # 6 notify_and_page calls within the last 60 minutes, threshold 5 -> True.
    entries = [_notify_entry(REFERENCE_NOW - timedelta(minutes=m)) for m in (1, 5, 10, 20, 30, 45)]
    _write_audit_log(log_path, entries)

    assert (
        detect_anomalous_activity(log_path, window_minutes=60, escalation_threshold=5, now=REFERENCE_NOW)
        is True
    )


def test_entries_outside_window_are_not_counted(tmp_path):
    log_path = tmp_path / "audit_log.jsonl"
    # 6 total notify_and_page calls, but only 2 fall within the trailing window.
    entries = [_notify_entry(REFERENCE_NOW - timedelta(minutes=m)) for m in (5, 10, 120, 200, 300, 400)]
    _write_audit_log(log_path, entries)

    assert (
        detect_anomalous_activity(log_path, window_minutes=60, escalation_threshold=1, now=REFERENCE_NOW)
        is True
    )
    assert (
        detect_anomalous_activity(log_path, window_minutes=60, escalation_threshold=2, now=REFERENCE_NOW)
        is False
    )


def test_non_notify_entries_are_ignored(tmp_path):
    log_path = tmp_path / "audit_log.jsonl"
    entries = [_other_entry(REFERENCE_NOW - timedelta(minutes=m)) for m in (1, 2, 3, 4, 5, 6)]
    _write_audit_log(log_path, entries)

    assert (
        detect_anomalous_activity(log_path, window_minutes=60, escalation_threshold=1, now=REFERENCE_NOW)
        is False
    )


def test_malformed_lines_are_skipped_not_fatal(tmp_path):
    log_path = tmp_path / "audit_log.jsonl"
    good_entries = [_notify_entry(REFERENCE_NOW - timedelta(minutes=m)) for m in (1, 2, 3)]
    lines = [json.dumps(e) for e in good_entries]
    lines.insert(1, "not valid json {{{")
    log_path.write_text("\n".join(lines) + "\n")

    assert (
        detect_anomalous_activity(log_path, window_minutes=60, escalation_threshold=2, now=REFERENCE_NOW)
        is True
    )
