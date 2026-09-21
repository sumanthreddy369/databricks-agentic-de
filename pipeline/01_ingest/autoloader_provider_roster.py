"""Databricks-only DLT ingest step. Not executed in this environment — requires
a real cluster with `dlt` and cloud storage credentials.

The provider roster is a slow-changing reference table (new hires, specialty
changes) that our own simulator drops as JSON batch files into a landing
directory (see simulator/autoloader_feed.py). Autoloader is the right tool
here (vs. Kafka) because this data is batchy and file-shaped, not an event
stream — using cloudFiles avoids re-scanning the whole landing directory on
every trigger and evolves the schema automatically as new roster columns
appear.
"""

import dlt
from pyspark.sql import SparkSession

from pipeline.common.schemas import PROVIDER_ROSTER_SCHEMA

spark = SparkSession.getActiveSession()

PROVIDER_LANDING_PATH = spark.conf.get(
    "pipeline.provider_landing_path", "/Volumes/healthcare_agentic_de/landing/providers"
)


@dlt.table(
    name="raw_provider_roster",
    comment="Provider roster JSON batch files picked up via Autoloader from the landing volume.",
)
def raw_provider_roster():
    return (
        spark.readStream.format("cloudFiles")
        .option("cloudFiles.format", "json")
        .option("cloudFiles.schemaEvolutionMode", "addNewColumns")
        .schema(PROVIDER_ROSTER_SCHEMA)
        .load(PROVIDER_LANDING_PATH)
    )
