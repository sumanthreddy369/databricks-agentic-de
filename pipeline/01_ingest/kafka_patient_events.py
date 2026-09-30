"""Databricks-only DLT ingest step. Not executed in this environment — requires
a real cluster with `dlt` and a reachable Kafka-compatible broker (Redpanda in
local dev via docker-compose, a managed Kafka/Confluent cluster in prod).

Reads the raw `patient_events` topic as a streaming source and parses the JSON
payload into the typed schema defined in pipeline/common/schemas.py. This is
the only place PERMISSIVE-mode JSON parsing happens — anything that doesn't
match the schema lands in `_corrupt_record` rather than crashing ingest;
Bronze is expected to carry that column through untouched (see
02_bronze/bronze_patient_events.py).
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

KAFKA_BOOTSTRAP_SERVERS = spark.conf.get(
    "pipeline.kafka_bootstrap_servers", "localhost:19092"
)


@dlt.table(
    name="raw_patient_events",
    comment="Raw Kafka payloads from the patient_events topic, JSON-parsed with permissive error handling.",
)
def raw_patient_events():
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
            F.from_json(
                F.col("value").cast("string"),
                PATIENT_EVENT_SCHEMA,
                options={"mode": "PERMISSIVE", "columnNameOfCorruptRecord": "_corrupt_record"},
            ).alias("payload"),
        )
    )
