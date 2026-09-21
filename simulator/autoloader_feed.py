"""Periodically writes provider-roster JSON batch files into a landing
directory, standing in for a real HR/credentialing system export that
Autoloader (pipeline/01_ingest/autoloader_provider_roster.py) would pick up
from cloud storage.

    python -m simulator.autoloader_feed --interval 30 --batches 5
"""

import json
import random
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.console import Console

app = typer.Typer(add_completion=False)
console = Console()

DEFAULT_LANDING_PATH = "data/landing/providers"

_SPECIALTIES = ("Cardiology", "Internal Medicine", "Emergency Medicine", "Oncology", "Critical Care")
_UNITS = ("ED", "ICU", "MedSurg", "Cardiology", "Oncology")
_FIRST_NAMES = ("Alex", "Jordan", "Taylor", "Morgan", "Casey", "Riley")
_LAST_NAMES = ("Nguyen", "Patel", "Kim", "Lopez", "Chen", "Okafor")


def build_provider_batch(size: int = 5) -> list[dict]:
    """Pure: returns a list of provider roster row dicts."""
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return [
        {
            "provider_id": f"prov_{random.randint(0, 999):03d}",
            "full_name": f"Dr. {random.choice(_FIRST_NAMES)} {random.choice(_LAST_NAMES)}",
            "specialty": random.choice(_SPECIALTIES),
            "npi": f"{random.randint(1000000000, 9999999999)}",
            "home_unit": random.choice(_UNITS),
            "updated_at": now,
        }
        for _ in range(size)
    ]


def write_batch_file(landing_dir: Path, batch: list[dict]) -> Path:
    landing_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    path = landing_dir / f"providers_batch_{timestamp}_{uuid.uuid4().hex[:6]}.json"
    path.write_text(json.dumps(batch, indent=2))
    return path


@app.command()
def run(
    landing_path: str = typer.Option(DEFAULT_LANDING_PATH, help="Directory to drop batch files into."),
    interval: int = typer.Option(30, help="Seconds between batch drops."),
    batches: int = typer.Option(5, help="Number of batches to write before exiting."),
    batch_size: int = typer.Option(5, help="Rows per batch."),
) -> None:
    landing_dir = Path(landing_path)
    console.print(f"[bold green]Writing {batches} provider batches to {landing_dir}[/]")
    for i in range(batches):
        path = write_batch_file(landing_dir, build_provider_batch(batch_size))
        console.print(f"wrote {path}")
        if i < batches - 1:
            time.sleep(interval)
    console.print("[bold green]Done.[/]")


if __name__ == "__main__":
    app()
