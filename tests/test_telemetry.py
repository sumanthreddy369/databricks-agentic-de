"""Proves the observability layer: agent/telemetry.py's metrics are computed
from the records (not separate counters), reach an OpenTelemetry reader,
the Grafana dashboard only queries metrics that exist, and orchestrator
spans carry outcomes but never request text. No network: in-memory
exporter and reader throughout."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agent import otel, telemetry
from agent.llm import ToolResult
from agent.orchestrator import OrchestratorAgent

REPO = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


def _line(minutes_ago, request_id, mode, tool, output):
    return {
        "ts": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
        "request_id": request_id,
        "mode": mode,
        "tool": tool,
        "input": {},
        "output": json.dumps(output),
        "is_error": False,
    }


AUDIT = [
    _line(50, "r1", "da", "compute_metric", {"rows": [{"value": 34}]}),
    _line(45, "r2", "da", "ask_genie", {"ok": False, "refused": True}),
    _line(30, "r3", "de", "lookup_problem", {"matches": [{"id": "B2"}]}),
    _line(30, "r3", "de", "notify_and_page", {"ok": True}),
    _line(10, "r4", "de", "quarantine_bad_records", {"pending_approval": True, "approval_id": "apr_1"}),
]
STATE = {
    "autonomous_remediation_enabled": False,
    "pending_approvals": [
        {
            "id": "apr_1",
            "tool": "quarantine_bad_records",
            "input": {"table": "silver_vitals", "expectation": "plausible_vital_value"},
            "status": "executed",
            "requested_at": (NOW - timedelta(minutes=10)).isoformat(),
            "decided_at": (NOW - timedelta(minutes=4)).isoformat(),
            "decided_by": "Jane Doe",
        },
        {
            "id": "apr_2",
            "tool": "restart_pipeline",
            "input": {"pipeline_name": "streaming_patient_pipeline"},
            "status": "pending",
            "requested_at": (NOW - timedelta(minutes=2)).isoformat(),
            "expires_at": (NOW + timedelta(hours=4)).isoformat(),
        },
    ],
}
GOLDEN = {
    "run_at": "20261014T021229Z",
    "rescored_from": "20261014T021229Z",
    "summary": {
        "genie": {"correct": 21, "pass": 5, "fail": 1, "questions": 27},
        "genie_guarded": {"correct": 21, "pass": 6, "questions": 27},
        "genie_latency_s": {"p50": 13.4, "p90": 17.1, "max": 22.1},
    },
}
MODEL_EVAL = {
    "suites": {
        "physiology": {
            "sets": {
                "stable": {"n": 3000, "should_flag": False, "flag_rate": {"news2": 0.018, "model_v2": 0.048}}
            },
            "summary": {"news2": {"recall": 0.474, "false_positive_rate": 0.018}},
        }
    },
    "decision": {"recall_at_matched_false_alarms": {"news2": 0.474, "model_v2": 0.607}},
}


def _metrics(rows):
    return {(name, tuple(sorted(labels.items()))): value for name, labels, value in rows}


def test_snapshot_is_computed_from_the_records():
    m = _metrics(telemetry.snapshot(AUDIT, STATE, GOLDEN, MODEL_EVAL, now=NOW))

    assert m[("agentic_de_requests", (("mode", "da"),))] == 2
    assert m[("agentic_de_requests", (("mode", "de"),))] == 2
    assert m[("agentic_de_requests_needing_person", (("reason", "escalation"),))] == 1
    assert m[("agentic_de_requests_needing_person", (("reason", "approval"),))] == 1
    assert m[("agentic_de_share_handled_without_person", ())] == 0.5
    assert m[("agentic_de_safety_events", (("event", "identifier requests refused"),))] == 1
    assert m[("agentic_de_problems", (("id", "B2"), ("title", "Hard-stop contract break")))] == 1
    assert m[("agentic_de_approvals", (("outcome", "executed"),))] == 1
    assert m[("agentic_de_approval_decision_minutes", ())] == 6.0
    assert m[("agentic_de_approvals_waiting", ())] == 1
    assert m[("agentic_de_kill_switch_engaged", ())] == 1
    assert m[("agentic_de_action_classes", (("policy", "approval"),))] == 2
    assert m[("agentic_de_golden_results", (("config", "genie"), ("result", "fail")))] == 1
    assert ("agentic_de_golden_results", (("config", "genie"), ("result", "questions"))) not in m
    assert m[("agentic_de_answer_latency_seconds", (("path", "genie"), ("stat", "p50")))] == 13.4
    assert m[("agentic_de_golden_run_age_hours", ())] == round(
        (NOW - datetime(2026, 10, 14, 2, 12, 29, tzinfo=UTC)).total_seconds() / 3600, 2
    )
    flag = (
        "agentic_de_anomaly_eval_flag_rate",
        (("detector", "model_v2"), ("suite", "physiology"), ("test_set", "stable")),
    )
    assert m[flag] == 0.048
    assert m[("agentic_de_anomaly_detector_recall", (("detector", "news2"), ("suite", "physiology")))] == 0.474
    assert m[("agentic_de_anomaly_recall_at_matched_false_alarms", (("detector", "model_v2"),))] == 0.607


def test_every_snapshot_metric_is_declared():
    names = {name for name, _, _ in telemetry.snapshot(AUDIT, STATE, GOLDEN, MODEL_EVAL, now=NOW)}
    assert names <= set(telemetry.METRICS)


def test_empty_records_produce_no_ratio_and_no_crash():
    m = _metrics(telemetry.snapshot([], {}, None, None, now=NOW))
    assert ("agentic_de_share_handled_without_person", ()) not in m
    assert m[("agentic_de_approvals_waiting", ())] == 0
    assert m[("agentic_de_kill_switch_engaged", ())] == 0


def test_gauges_reach_an_opentelemetry_reader(tmp_path):
    audit = tmp_path / "audit.jsonl"
    audit.write_text("\n".join(json.dumps(line) for line in AUDIT))
    state = tmp_path / "state.json"
    state.write_text(json.dumps(STATE))
    reader = InMemoryMetricReader()
    telemetry.build_meter_provider(telemetry._Source(audit, state, days=36500), reader)

    data = reader.get_metrics_data()
    points = {
        (metric.name, tuple(sorted(point.attributes.items()))): point.value
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for metric in sm.metrics
        for point in metric.data.data_points
    }
    assert points[("agentic_de_requests", (("mode", "da"),))] == 2
    assert points[("agentic_de_approvals_waiting", ())] == 1


def test_newest_golden_report_prefers_the_rescored_copy(tmp_path):
    (tmp_path / "golden_a.json").write_text(json.dumps({"run_at": "20261014T020349Z", "summary": {}}))
    (tmp_path / "golden_b.json").write_text(json.dumps({"run_at": "20261014T021229Z", "summary": {"x": 1}}))
    (tmp_path / "golden_b_rescored.json").write_text(
        json.dumps({"run_at": "20261014T021229Z", "rescored_from": "20261014T021229Z", "summary": {"x": 2}})
    )
    (tmp_path / "golden_broken.json").write_text("not json")

    assert telemetry.newest_golden_report([tmp_path])["summary"] == {"x": 2}


def test_dashboard_is_current_and_only_queries_declared_metrics():
    import re
    import sys

    sys.path.insert(0, str(REPO / "scripts"))
    import build_grafana_dashboard

    committed = (REPO / "observability" / "grafana" / "dashboards" / "agentic_de.json").read_text(encoding="utf-8")
    assert committed == build_grafana_dashboard.render(), "run scripts/build_grafana_dashboard.py"
    queried = set(re.findall(r"agentic_de_\w+", committed))
    assert queried and queried <= set(telemetry.METRICS)


def test_tool_outcome_reads_flags_only():
    assert otel.tool_outcome(json.dumps({"refused": True}), False) == "refused"
    assert otel.tool_outcome(json.dumps({"withheld": True}), False) == "withheld"
    assert otel.tool_outcome(json.dumps({"pending_approval": True}), False) == "pending_approval"
    assert otel.tool_outcome(json.dumps({"guardrails": {"suppressed_cells": 2}}), False) == "suppressed"
    assert otel.tool_outcome("<untrusted_data>x</untrusted_data>", False) == "untrusted_wrapped"
    assert otel.tool_outcome("not json", True) == "error"
    assert otel.tool_outcome(json.dumps({"ok": True}), False) == "ok"


class _Scripted:
    def __init__(self, steps):
        self.steps = steps

    def run_tool_loop(self, system, messages, tools, dispatch, max_turns=6):
        for name, tool_input in self.steps:
            dispatch(name, tool_input)
        return "done"


class _Bridge:
    def dispatch(self, tool_name, tool_input):
        return ToolResult(tool_use_id="", content=json.dumps({"ok": True}))


def test_request_and_tool_spans_carry_outcomes_but_never_request_text(tmp_path):
    exporter = InMemorySpanExporter()
    otel.configure(exporter=exporter)
    question = "What is the full name of patient pt_00052?"
    agent = OrchestratorAgent(
        claude=_Scripted([("ask_genie", {"question": question})]),
        state_path=tmp_path / "state.json",
        audit_log_path=tmp_path / "audit.jsonl",
        mcp_bridge=_Bridge(),
    )

    agent.handle(question, mode="da")

    spans = {s.name: s for s in exporter.get_finished_spans()}
    request, tool = spans["agent.request"], spans["agent.tool ask_genie"]
    assert tool.parent.span_id == request.context.span_id
    assert tool.attributes["tool.name"] == "ask_genie"
    assert tool.attributes["tool.outcome"] == "refused"
    assert request.attributes["agent.mode"] == "da"
    assert request.attributes["agent.tool_calls"] == 1
    every_value = " ".join(str(v) for s in exporter.get_finished_spans() for v in s.attributes.values())
    assert "pt_00052" not in every_value and "full name" not in every_value
    otel.configure()


def test_demo_traffic_drives_the_real_guardrails_and_approval_queue(tmp_path, monkeypatch):
    import sys

    sys.path.insert(0, str(REPO / "scripts"))
    import demo_traffic

    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    counts = demo_traffic.run(1, demo_dir=tmp_path)

    assert counts == {"analyst": 6, "health_checks": 3, "approved": 1, "rejected": 1}
    lines = [json.loads(x) for x in (tmp_path / "audit_log.jsonl").read_text(encoding="utf-8").splitlines()]
    m = _metrics(
        telemetry.snapshot(
            lines, json.loads((tmp_path / "pipeline_state.json").read_text()), None, None, now=datetime.now(UTC)
        )
    )
    assert m[("agentic_de_safety_events", (("event", "identifier requests refused"),))] == 2
    assert m[("agentic_de_approvals", (("outcome", "executed"),))] == 1
    assert m[("agentic_de_approvals", (("outcome", "rejected"),))] == 1


WORKSPACE = {
    "measured_at": "2026-10-14T10:00:00Z",
    "tables": [{"table": "gold.dim_patients", "type": "MATERIALIZED_VIEW", "rows": 1359}],
    "expectations": [
        {"dataset": "silver_fct_vitals", "expectation": "plausible_vital_value", "passed": 99352, "failed": 241}
    ],
    "masks": {"patients": 1359, "names_masked": 1359, "mrns_masked": 1358},
    "identity_check": {"patient_ids_admitted_more_than_once": 23, "flagged_two_people_one_id": 23},
}


def test_workspace_snapshot_becomes_pipeline_health_metrics():
    m = _metrics(telemetry.snapshot([], {}, None, None, now=NOW, workspace=WORKSPACE))

    assert m[("agentic_de_pipeline_table_rows", (("table", "gold.dim_patients"),))] == 1359
    failed = (("dataset", "silver_fct_vitals"), ("expectation", "plausible_vital_value"), ("result", "failed"))
    assert m[("agentic_de_pipeline_expectation_records", failed)] == 241
    # One unmasked MRN must show as less than 100% - the weaker of the two masks counts.
    assert m[("agentic_de_phi_masked_share", ())] == round(1358 / 1359, 4)
    assert m[("agentic_de_identity_conflicts_flagged", ())] == 23
    assert m[("agentic_de_pipeline_snapshot_age_hours", ())] == 2.0


def test_newest_workspace_snapshot_is_picked(tmp_path):
    (tmp_path / "workspace_a.json").write_text(json.dumps({"measured_at": "2026-10-13T00:00:00Z", "tables": [1]}))
    (tmp_path / "workspace_b.json").write_text(json.dumps({"measured_at": "2026-10-14T00:00:00Z", "tables": [2]}))
    assert telemetry.newest_workspace_snapshot([tmp_path])["tables"] == [2]
