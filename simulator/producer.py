"""Kafka(Redpanda) producer CLI for the patient_events stream.

    python -m simulator.producer --patients 1000 --duration 60

Advances a simulated clock, calls Population.tick(now) each step, serializes
each event to JSON, and publishes to KAFKA_TOPIC_PATIENT_EVENTS keyed by
encounter_id (or patient_id for `admitted`, since the encounter doesn't exist
in any downstream partition state yet at that point).

`build_event_payload` is a pure function (dataclass -> bytes) kept separate
from `send()` (which needs a real broker) so it's directly unit-testable.

Two guardrails live here: `_produce_with_retry` wraps the actual
`producer.produce(...)` call in exponential-backoff retry (tenacity) so a
transient broker blip doesn't crash the whole simulator run, and `RateLimiter`
implements the `--max-events-per-second` option, a simple token-bucket
per-source rate limit distinct from `simulator/population.py`'s per-vital
cadence jitter.

`send()` (the publish loop's per-event call) is wrapped in an OpenTelemetry
span per event published — see `agent/otel.py` for the no-op-unless-configured
exporter pattern this uses; by default this creates spans but exports them
nowhere (zero network calls), same as everywhere else this pattern appears.
"""

import dataclasses
import json
import time
from datetime import UTC, datetime, timedelta

import tenacity
import typer
from rich.console import Console

from agent.otel import get_tracer
from common.contracts import KAFKA_TOPIC_PATIENT_EVENTS
from simulator.domain import PatientEvent
from simulator.population import Population

app = typer.Typer(add_completion=False)
console = Console()

TICK_SECONDS = 1.0

# Retry guardrail: a flaky broker connection (network blip, broker mid-restart)
# should not crash the whole simulator run. Retries only exceptions shaped
# like a transient/network problem — BufferError is confluent-kafka's
# "local queue is full, try again" signal, OSError covers socket-level
# failures. A genuine misconfiguration (unknown topic, auth failure) isn't
# something 3 retries fixes, but we don't have a narrower confluent-kafka
# exception type to distinguish that from a transient outage without a live
# broker to observe, so this is documented as best-effort, not exhaustive.
_TRANSIENT_PRODUCE_EXCEPTIONS = (BufferError, OSError)


@tenacity.retry(
    retry=tenacity.retry_if_exception_type(_TRANSIENT_PRODUCE_EXCEPTIONS),
    stop=tenacity.stop_after_attempt(3),
    wait=tenacity.wait_exponential(multiplier=0.5, max=4),
    reraise=True,
)
def _produce_with_retry(producer, topic: str, key: bytes, value: bytes) -> None:
    producer.produce(topic, key=key, value=value)


class RateLimiter:
    """Simple token-bucket throttle for `--max-events-per-second`.

    Distinct from the per-vital jitter in `simulator/population.py`'s
    `Population.tick()` (which spreads out when a given vital *type* next
    fires for one encounter): this caps the *aggregate* publish rate across
    the whole simulator process, the same shape as a per-source rate limit a
    real ingestion gateway would apply. `max_per_second=None` disables
    throttling entirely (the default, preserving existing behavior).
    """

    def __init__(self, max_per_second: float | None) -> None:
        self.max_per_second = max_per_second
        self._tokens = float(max_per_second) if max_per_second else 0.0
        self._last = time.monotonic()

    def acquire(self, n: int = 1) -> None:
        if not self.max_per_second:
            return
        while True:
            now = time.monotonic()
            elapsed = now - self._last
            self._last = now
            self._tokens = min(self.max_per_second, self._tokens + elapsed * self.max_per_second)
            if self._tokens >= n:
                self._tokens -= n
                return
            time.sleep((n - self._tokens) / self.max_per_second)


def _asdict_or_none(value) -> dict | None:
    if value is None:
        return None
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    return value


def build_event_payload(event: PatientEvent) -> bytes:
    """Pure: PatientEvent -> UTF-8 JSON bytes matching the wire envelope."""
    envelope = {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "event_ts": event.event_ts.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "patient_id": event.patient_id,
        "encounter_id": event.encounter_id,
        "schema_version": event.schema_version,
        "patient": _asdict_or_none(event.patient),
        "encounter": _asdict_or_none(event.encounter),
        "transfer": event.transfer,
        "vital": _asdict_or_none(event.vital),
        "notes": event.notes,
    }
    return json.dumps(envelope).encode("utf-8")


def _event_key(event: PatientEvent) -> bytes:
    key = event.patient_id if event.event_type == "admitted" else event.encounter_id
    return key.encode("utf-8")


def _make_producer(bootstrap_servers: str):
    from confluent_kafka import Producer

    return Producer({"bootstrap.servers": bootstrap_servers})


def send(producer, event: PatientEvent, topic: str = KAFKA_TOPIC_PATIENT_EVENTS) -> None:
    tracer = get_tracer()
    span_attributes = {
        "event.type": event.event_type,
        "event.patient_id": event.patient_id,
        "event.encounter_id": event.encounter_id,
        "messaging.destination": topic,
    }
    with tracer.start_as_current_span("publish_patient_event", attributes=span_attributes):
        _produce_with_retry(producer, topic, key=_event_key(event), value=build_event_payload(event))
        producer.poll(0)


@app.command()
def run(
    patients: int = typer.Option(1000, help="Target concurrently-admitted patient population."),
    duration: int = typer.Option(60, help="How many simulated seconds to run for."),
    bootstrap_servers: str = typer.Option("localhost:19092", help="Kafka/Redpanda bootstrap servers."),
    topic: str = typer.Option(KAFKA_TOPIC_PATIENT_EVENTS, help="Target topic."),
    max_events_per_second: float | None = typer.Option(
        None,
        "--max-events-per-second",
        help="Rate-limit guardrail: cap the aggregate publish rate (events/sec) across the whole run. "
        "Unset means unlimited, i.e. current behavior.",
    ),
) -> None:
    population = Population(target_size=patients)
    now = datetime.now(UTC)
    producer = _make_producer(bootstrap_servers)
    limiter = RateLimiter(max_events_per_second)

    console.print(f"[bold green]Starting simulator[/] patients={patients} duration={duration}s topic={topic}")
    initial_events = population.admit_new_patients(patients, now)
    for event in initial_events:
        limiter.acquire()
        send(producer, event, topic)
    console.print(f"Admitted {len(initial_events)} initial patients.")

    elapsed = 0
    while elapsed < duration:
        now += timedelta(seconds=TICK_SECONDS)
        for event in population.tick(now):
            limiter.acquire()
            send(producer, event, topic)
        producer.flush(0)
        time.sleep(TICK_SECONDS)
        elapsed += TICK_SECONDS

    producer.flush(5)
    console.print("[bold green]Done.[/]")


if __name__ == "__main__":
    app()
