"""Databricks-only DLT ingest step for patient events.

Two interchangeable sources, chosen by the pipeline setting
`pipeline.patient_events_source`:

- `kafka` (production): the `patient_events` topic on a Kafka-compatible
  broker the workspace can reach (a managed Kafka/Confluent cluster).
- `files` (free trial / dev): JSON-lines files the simulator writes and
  uploads to a Unity Catalog volume (`simulator/file_feed.py`), picked up by
  Autoloader. A cloud pipeline can't reach the Redpanda broker on a laptop,
  and this needs no extra service or account.

Both produce exactly the same columns, so Bronze and everything after it
don't know which source ran: `kafka_key`, `kafka_timestamp` (the arrival
time), `payload` (the event parsed with pipeline/common/schemas.py), and
`_corrupt_record`. `_corrupt_record` holds the raw text of any message that
didn't parse into an event - one where the payload and its key fields came
back empty - so Bronze can keep it rather than lose it. Bronze reads this
column; it was never produced before this file computed it, which would have
failed the first real run.

Never run on Databricks in this environment.
"""

import sys

import dlt
from pyspark.sql import SparkSession
from pyspark.sql import functions as F

spark = SparkSession.getActiveSession()

# DLT doesn't put the bundle root on sys.path, so project imports need it
# added explicitly; resources/dlt_pipeline.yml sets bundle.sourcePath.
sys.path.append(spark.conf.get("bundle.sourcePath", "."))

from common.contracts import KAFKA_TOPIC_PATIENT_EVENTS  # noqa: E402
from pipeline.common.schemas import PATIENT_EVENT_SCHEMA  # noqa: E402

PATIENT_EVENTS_SOURCE = spark.conf.get("pipeline.patient_events_source", "kafka")
KAFKA_BOOTSTRAP_SERVERS = spark.conf.get("pipeline.kafka_bootstrap_servers", "localhost:19092")
PATIENT_EVENTS_PATH = spark.conf.get(
    "pipeline.patient_events_path", "/Volumes/healthcare_agentic_de/bronze/landing/patient_events"
)


def _read_kafka():
    return (
        spark.readStream.format("kafka")
        .option("kafka.bootstrap.servers", KAFKA_BOOTSTRAP_SERVERS)
        .option("subscribe", KAFKA_TOPIC_PATIENT_EVENTS)
        .option("startingOffsets", "latest")
        .option("failOnDataLoss", "false")
        .load()
        .select(
            F.col("key").cast("string").alias("kafka_key"),
            F.col("timestamp").alias("kafka_timestamp"),
            F.col("value").cast("string").alias("_raw"),
        )
    )


def _read_files():
    # One JSON event per line; `text` keeps each raw line so a malformed one
    # can be kept in _corrupt_record exactly as it arrived.
    return (
        spark.readStream.format("cloudFiles")
        .option("cloudFiles.format", "text")
        .load(PATIENT_EVENTS_PATH)
        .select(
            F.lit(None).cast("string").alias("kafka_key"),
            F.col("_metadata.file_modification_time").alias("kafka_timestamp"),
            F.col("value").alias("_raw"),
        )
    )


@dlt.table(
    name="raw_patient_events",
    comment="Raw patient events (Kafka or landing files), JSON-parsed with permissive error handling.",
)
def raw_patient_events():
    raw = _read_files() if PATIENT_EVENTS_SOURCE == "files" else _read_kafka()
    parsed = raw.withColumn("payload", F.from_json(F.col("_raw"), PATIENT_EVENT_SCHEMA, {"mode": "PERMISSIVE"}))
    unparsed = F.col("payload").isNull() | (
        F.col("payload.event_id").isNull() & F.col("payload.event_type").isNull()
    )
    return parsed.select(
        "kafka_key",
        "kafka_timestamp",
        "payload",
        F.when(unparsed, F.col("_raw")).alias("_corrupt_record"),
    )
