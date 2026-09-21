"""Databricks-only DLT bronze step. Not executed in this environment.

Bronze flattens the parsed payload struct and stamps an ingestion time, but
never rejects a row — even a malformed/corrupt record is retained (with
`_corrupt_record` populated) so nothing is silently lost before Silver gets a
chance to apply real data-quality rules. `dlt.expect` here is warn-only,
used purely for pipeline observability metrics (see
agent/tools/pipeline_health.check_expectation_metrics), never to drop rows.
"""

import dlt
from pyspark.sql import functions as F


@dlt.table(
    name="bronze_patient_events",
    comment="Flattened patient_events payload, one row per raw Kafka message. Never drops rows.",
)
@dlt.expect("has_event_id", "payload.event_id IS NOT NULL")
@dlt.expect("has_event_type", "payload.event_type IS NOT NULL")
def bronze_patient_events():
    raw = dlt.read_stream("raw_patient_events")
    return raw.select(
        F.col("payload.event_id").alias("event_id"),
        F.col("payload.event_type").alias("event_type"),
        F.col("payload.event_ts").alias("event_ts"),
        F.col("payload.patient_id").alias("patient_id"),
        F.col("payload.encounter_id").alias("encounter_id"),
        F.col("payload.schema_version").alias("schema_version"),
        F.col("payload.patient").alias("patient"),
        F.col("payload.encounter").alias("encounter"),
        F.col("payload.transfer").alias("transfer"),
        F.col("payload.vital").alias("vital"),
        F.col("payload.notes").alias("notes"),
        F.col("_corrupt_record"),
        F.col("kafka_key"),
        F.col("kafka_timestamp"),
        F.current_timestamp().alias("_ingested_at"),
    )
