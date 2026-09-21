"""Kafka(Redpanda) producer CLI for the patient_events stream.

    python -m simulator.producer --patients 1000 --duration 60

Advances a simulated clock, calls Population.tick(now) each step, serializes
each event to JSON, and publishes to KAFKA_TOPIC_PATIENT_EVENTS keyed by
encounter_id (or patient_id for `admitted`, since the encounter doesn't exist
in any downstream partition state yet at that point).

`build_event_payload` is a pure function (dataclass -> bytes) kept separate
from `send()` (which needs a real broker) so it's directly unit-testable.
"""

import dataclasses
import json
import time
from datetime import UTC, datetime, timedelta

import typer
from rich.console import Console

from common.contracts import KAFKA_TOPIC_PATIENT_EVENTS
from simulator.domain import PatientEvent
from simulator.population import Population

app = typer.Typer(add_completion=False)
console = Console()

TICK_SECONDS = 1.0


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
    producer.produce(topic, key=_event_key(event), value=build_event_payload(event))
    producer.poll(0)


@app.command()
def run(
    patients: int = typer.Option(1000, help="Target concurrently-admitted patient population."),
    duration: int = typer.Option(60, help="How many simulated seconds to run for."),
    bootstrap_servers: str = typer.Option("localhost:19092", help="Kafka/Redpanda bootstrap servers."),
    topic: str = typer.Option(KAFKA_TOPIC_PATIENT_EVENTS, help="Target topic."),
) -> None:
    population = Population(target_size=patients)
    now = datetime.now(UTC)
    producer = _make_producer(bootstrap_servers)

    console.print(f"[bold green]Starting simulator[/] patients={patients} duration={duration}s topic={topic}")
    initial_events = population.admit_new_patients(patients, now)
    for event in initial_events:
        send(producer, event, topic)
    console.print(f"Admitted {len(initial_events)} initial patients.")

    elapsed = 0
    while elapsed < duration:
        now += timedelta(seconds=TICK_SECONDS)
        for event in population.tick(now):
            send(producer, event, topic)
        producer.flush(0)
        time.sleep(TICK_SECONDS)
        elapsed += TICK_SECONDS

    producer.flush(5)
    console.print("[bold green]Done.[/]")


if __name__ == "__main__":
    app()
