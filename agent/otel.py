"""OpenTelemetry tracing for this project's NON-LLM parts — the simulator's
Kafka publish loop (`simulator/producer.py:send`) and the DE-mode
pipeline-health tool calls (`agent/tools/pipeline_health.py`). LLM-call
tracing already has its own, separate mechanism (`agent/llm.py:Tracer`,
optional Langfuse spans) — this module is deliberately independent of that
one, covering the surrounding infrastructure a Langfuse trace wouldn't see.

Same no-op-unless-configured honesty pattern used throughout this project
(`agent/llm.py:Tracer` for Langfuse, `agent/secrets.py:get_secret` for GCP
Secret Manager): by default, no `OTEL_EXPORTER_OTLP_ENDPOINT` means spans are
still created (so instrumented code always runs the same code path, in tests
and in production alike) but nothing exports them anywhere — zero network
calls. Setting `OTEL_EXPORTER_OTLP_ENDPOINT` switches to a real OTLP/HTTP
exporter for an actual deployment; this has NOT been exercised against a real
OTel collector in this environment (no collector endpoint here) — written
defensively (falls back to the same no-op behavior, logging a warning, if the
OTLP exporter package isn't installed) for the same reason `agent/llm.py`'s
Langfuse spans are: tracing must never be able to break the thing it's
observing.

Tests call `configure(exporter=InMemorySpanExporter())` before exercising
instrumented code, then inspect `exporter.get_finished_spans()` — see
`tests/test_otel_tracing.py`.
"""

import os
import threading

import structlog
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter

logger = structlog.get_logger(__name__)

SERVICE_NAME = "databricks-agentic-de"

_provider: TracerProvider | None = None
_provider_lock = threading.Lock()


def configure(exporter: SpanExporter | None = None) -> TracerProvider:
    """(Re)builds the module-level `TracerProvider` and returns it.

    - `exporter` given explicitly (tests: an `InMemorySpanExporter`) — spans
      are exported to it via a `SimpleSpanProcessor` (synchronous, so a test
      can call `get_finished_spans()` immediately after, no batching delay).
    - `exporter=None` and `OTEL_EXPORTER_OTLP_ENDPOINT` is set — attempts a
      real OTLP/HTTP exporter, batched (`BatchSpanProcessor`), for an actual
      deployment. If the OTLP exporter package isn't installed, logs a
      warning and falls back to the fully-no-op case below rather than
      crashing the caller.
    - Neither — genuinely no span processor is attached. Spans are still
      created (so instrumented functions behave identically either way,
      which is what makes this safe/cheap to leave on by default) but are
      simply dropped once finished; zero network calls.
    """
    global _provider
    with _provider_lock:
        provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))

        if exporter is not None:
            provider.add_span_processor(SimpleSpanProcessor(exporter))
        else:
            endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
            if endpoint:
                _attach_otlp_exporter(provider, endpoint)

        _provider = provider
        return provider


def _attach_otlp_exporter(provider: TracerProvider, endpoint: str) -> None:
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    except Exception:
        logger.warning(
            "otlp_exporter_unavailable_spans_not_exported",
            endpoint=endpoint,
            hint="install opentelemetry-exporter-otlp-proto-http to enable real OTLP export",
        )


def get_tracer():
    """Returns a tracer against the current (lazily-configured-on-first-use)
    module-level `TracerProvider`. Never raises; never makes a network call
    unless `configure()` was previously called with a real OTLP endpoint
    configured and reachable.
    """
    global _provider
    if _provider is None:
        configure()
    return _provider.get_tracer(SERVICE_NAME)
