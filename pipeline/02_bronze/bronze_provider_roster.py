"""Databricks-only DLT bronze step. Not executed in this environment.

Passes the provider roster through untouched apart from an ingestion
timestamp — same "bronze never rejects a row" rule as the patient-events
bronze table.
"""

import dlt
from pyspark.sql import functions as F


@dlt.table(
    name="bronze_provider_roster",
    comment="Raw provider roster rows plus an ingestion timestamp. Never drops rows.",
)
@dlt.expect("has_provider_id", "provider_id IS NOT NULL")
def bronze_provider_roster():
    raw = dlt.read_stream("raw_provider_roster")
    return raw.withColumn("_ingested_at", F.current_timestamp())
