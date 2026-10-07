"""Writes simulated patient events as JSON-lines files for Autoloader.

The free alternative to Kafka for a cloud workspace: a Databricks pipeline
can't reach the Redpanda broker on a laptop, but it can read files from a
Unity Catalog volume. This uses the same Population and the same wire
payload (`simulator.producer.build_event_payload`) as the Kafka producer, so
the pipeline sees identical events either way.

Time is simulated: the run starts `duration` seconds in the past and steps
forward one tick at a time without sleeping, so every event_ts is at or
before now (no future timestamps for the pipeline's clock-skew checks) and a
10-minute feed takes seconds to generate. Events are grouped into one file per
`seconds_per_file` of simulated time, like arrivals in micro-batches.

    uv run python -m simulator.file_feed --patients 200 --duration 600
    databricks fs cp -r data/landing/patient_events \\
        dbfs:/Volumes/healthcare_agentic_de/bronze/landing/patient_events

Files are written locally; uploading them is a separate, explicit step.
"""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import typer
from rich.console import Console

from simulator.population import Population
from simulator.producer import build_event_payload

app = typer.Typer(add_completion=False)
console = Console()

DEFAULT_OUTPUT_DIR = "data/landing/patient_events"
TICK_SECONDS = 1


def _payload_at(event, simulated_now: datetime) -> bytes:
    # The domain generators stamp the wall clock (right for the live Kafka
    # producer); here the event must carry its simulated time, or every event
    # in a 10-minute feed would share the few seconds it took to generate.
    event.event_ts = simulated_now
    return build_event_payload(event)


def generate_files(
    output_dir: Path,
    *,
    patients: int,
    duration: int,
    seconds_per_file: int,
    end: datetime | None = None,
    population: Population | None = None,
) -> list[Path]:
    """Writes the feed and returns the files written, oldest first."""
    output_dir.mkdir(parents=True, exist_ok=True)
    population = population or Population(target_size=patients)
    end = end or datetime.now(UTC)
    now = end - timedelta(seconds=duration)

    written: list[Path] = []
    batch = [_payload_at(e, now) for e in population.admit_new_patients(patients, now)]
    batch_start = now

    def flush() -> None:
        if not batch:
            return
        stamp = batch_start.strftime("%Y%m%dT%H%M%S")
        path = output_dir / f"patient_events_{stamp}_{uuid.uuid4().hex[:6]}.jsonl"
        path.write_bytes(b"\n".join(batch) + b"\n")
        written.append(path)
        batch.clear()

    elapsed = 0
    while elapsed < duration:
        now += timedelta(seconds=TICK_SECONDS)
        elapsed += TICK_SECONDS
        batch.extend(_payload_at(e, now) for e in population.tick(now))
        if elapsed % seconds_per_file == 0:
            flush()
            batch_start = now
    flush()
    return written


@app.command()
def run(
    patients: int = typer.Option(200, help="Target concurrently-admitted patient population."),
    duration: int = typer.Option(600, help="Simulated seconds of activity, ending now."),
    seconds_per_file: int = typer.Option(60, help="Simulated seconds of events per file."),
    output_dir: str = typer.Option(DEFAULT_OUTPUT_DIR, help="Local directory to write files into."),
) -> None:
    files = generate_files(
        Path(output_dir), patients=patients, duration=duration, seconds_per_file=seconds_per_file
    )
    events = sum(len(f.read_bytes().splitlines()) for f in files)
    console.print(f"[bold green]Wrote {events} events in {len(files)} files to {output_dir}[/]")


if __name__ == "__main__":
    app()
