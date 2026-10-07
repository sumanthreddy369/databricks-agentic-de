"""Proves simulator/file_feed.py writes what the `files` ingest source
expects: JSON-lines files of the same wire payload the Kafka producer sends,
timestamps never in the future, one file per simulated batch window."""

import json
import random
from datetime import UTC, datetime

from common.contracts import ALLOWED_EVENT_TYPES, PATIENT_EVENT_ENVELOPE_FIELDS
from simulator.autoloader_feed import build_provider_batch, write_batch_file
from simulator.file_feed import generate_files
from simulator.population import Population

END = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def _events(files):
    return [json.loads(line) for f in files for line in f.read_text().splitlines() if line.strip()]


def test_files_hold_one_valid_event_per_line(tmp_path):
    files = generate_files(
        tmp_path,
        patients=20,
        duration=300,
        seconds_per_file=60,
        end=END,
        population=Population(20, random.Random(7)),
    )
    events = _events(files)

    assert len(files) >= 5
    assert all(f.suffix == ".jsonl" for f in files)
    assert len(events) > 20
    for event in events:
        assert set(PATIENT_EVENT_ENVELOPE_FIELDS) <= set(event)
        assert event["event_type"] in ALLOWED_EVENT_TYPES


def test_no_event_is_timestamped_in_the_future(tmp_path):
    files = generate_files(
        tmp_path,
        patients=10,
        duration=120,
        seconds_per_file=30,
        end=END,
        population=Population(10, random.Random(1)),
    )

    latest = max(datetime.fromisoformat(e["event_ts"].replace("Z", "+00:00")) for e in _events(files))
    assert latest <= END


def test_provider_batches_are_json_lines(tmp_path):
    path = write_batch_file(tmp_path, build_provider_batch(3))

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 3
    assert all("provider_id" in row for row in rows)
