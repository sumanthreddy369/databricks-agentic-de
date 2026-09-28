"""Proves OpenTelemetry spans are actually created (not just plumbed and
never exercised) for the two instrumented, non-LLM code paths: the
simulator's publish loop (`simulator/producer.py:send`) and the DE-mode
pipeline-health tool calls (`agent/tools/pipeline_health.py`). Uses
`InMemorySpanExporter` throughout — zero network calls, `OTEL_EXPORTER_
OTLP_ENDPOINT` stays unset.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agent import otel
from agent.tools import pipeline_health
from simulator.domain import generate_vitals_event
from simulator.producer import send

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_STATE = REPO_ROOT / "data" / "state" / "pipeline_state.example.json"


def _configure_in_memory_exporter() -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    otel.configure(exporter=exporter)
    return exporter


def test_get_tracer_defaults_to_no_op_export_without_otel_endpoint(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    otel.configure()  # no exporter passed, no endpoint set -> nothing attached

    tracer = otel.get_tracer()
    with tracer.start_as_current_span("some_span"):
        pass  # must not raise, must not attempt any network call


def test_pipeline_health_tool_call_creates_a_span_with_expected_attributes(tmp_path):
    exporter = _configure_in_memory_exporter()
    state_path = tmp_path / "pipeline_state.json"
    state_path.write_text(EXAMPLE_STATE.read_text())

    result = pipeline_health.check_expectation_metrics(state_path)

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "check_expectation_metrics"
    assert spans[0].attributes["tool.is_error"] == result.is_error


def test_each_pipeline_health_tool_call_creates_its_own_span(tmp_path):
    exporter = _configure_in_memory_exporter()
    state_path = tmp_path / "pipeline_state.json"
    state_path.write_text(EXAMPLE_STATE.read_text())

    pipeline_health.check_expectation_metrics(state_path)
    pipeline_health.check_job_status(state_path)
    pipeline_health.notify_and_page(state_path, "test incident")

    span_names = [span.name for span in exporter.get_finished_spans()]
    assert span_names == ["check_expectation_metrics", "check_job_status", "notify_and_page"]


def test_quarantine_error_path_still_produces_a_span_flagged_as_error(tmp_path):
    exporter = _configure_in_memory_exporter()
    state_path = tmp_path / "pipeline_state.json"
    state_path.write_text(json.dumps({"tables": {}, "jobs": {}, "schema_snapshot": {}, "incidents": []}))

    result = pipeline_health.quarantine_bad_records(state_path, "nope", "nope")

    assert result.is_error is True
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].attributes["tool.is_error"] is True


def test_publish_event_creates_a_span_per_event(monkeypatch):
    exporter = _configure_in_memory_exporter()
    fake_producer = MagicMock()
    event = generate_vitals_event("pt_00001", "enc_00001", "heart_rate")

    send(fake_producer, event, topic="patient_events")
    send(fake_producer, event, topic="patient_events")

    spans = exporter.get_finished_spans()
    assert len(spans) == 2
    assert all(span.name == "publish_patient_event" for span in spans)
    assert spans[0].attributes["event.type"] == "vitals_reading"
    assert spans[0].attributes["event.patient_id"] == "pt_00001"
    assert spans[0].attributes["messaging.destination"] == "patient_events"
    fake_producer.poll.assert_called_with(0)
