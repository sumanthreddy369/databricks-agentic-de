"""Databricks-only DLT gold step. Not executed in this environment.

Gold materializes the Silver dimension/fact as-is for this domain — the
"business ready" transformation for encounters is really the apply_changes
logic already done in Silver, so Gold here is a thin passthrough that gives
BI/agent consumers a stable, documented table name and comment.

Gold tables are published by fully-qualified name into
`common.contracts.GOLD_SCHEMA` (the pipeline's default schema is `silver`,
see resources/dlt_pipeline.yml), because that's the only schema the
`orchestrator_agent` group can SELECT from and the schema the Unity Catalog
masks/row filters are declared against. Their Silver sources carry distinct
`silver_`-prefixed names: one DLT pipeline can't define two datasets with the
same name, even in different schemas.
"""

import sys

import dlt
from pyspark.sql import SparkSession
from pyspark.sql import functions as F

# DLT doesn't put the bundle root on sys.path, so project imports need it
# added explicitly; resources/dlt_pipeline.yml sets bundle.sourcePath.
sys.path.append(SparkSession.getActiveSession().conf.get("bundle.sourcePath", "."))

from common.contracts import GOLD_SCHEMA  # noqa: E402


@dlt.table(
    name=f"{GOLD_SCHEMA}.dim_patients",
    comment="Gold: one row per patient, PHI-masked at the Unity Catalog layer.",
)
def dim_patients():
    return dlt.read("silver_dim_patients")


@dlt.table(
    name=f"{GOLD_SCHEMA}.fct_encounters",
    comment="Gold: current state of every encounter (admit/transfer/discharge).",
)
def fct_encounters():
    return dlt.read("silver_fct_encounters")


@dlt.table(
    name=f"{GOLD_SCHEMA}.fct_encounter_history",
    comment=(
        "Gold: every version of every encounter, valid from valid_from until valid_to (NULL = current). "
        "Answers point-in-time questions such as the census of a unit at a given time."
    ),
)
def fct_encounter_history():
    return dlt.read("silver_fct_encounter_history").select(
        "encounter_id",
        "patient_id",
        "encounter_type",
        "unit",
        "attending_provider_id",
        "status",
        F.col("__START_AT").alias("valid_from"),
        F.col("__END_AT").alias("valid_to"),
    )
