"""Lakeflow (DLT) gold step. Runs on the GCP trial workspace.

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

Governance is declared here, in the table definitions, not with ALTER TABLE
afterwards: these Gold tables are pipeline-managed materialized views, and
Unity Catalog rejects `ALTER TABLE ... SET MASK` on them ("expects a table
but ... is a view" - found on the first real deploy). So:
- dim_patients declares column masks on full_name and mrn in its schema;
- fct_encounters and fct_encounter_history declare the unit row filter.
The mask and filter functions are created beforehand by
governance/05_unity_catalog/row_filters_and_masking.sql; the pipeline fails
to build these tables if they don't exist, which is the safe direction.
"""

import sys

import dlt
from pyspark.sql import SparkSession
from pyspark.sql import functions as F

# DLT doesn't put the bundle root on sys.path, so project imports need it
# added explicitly; resources/dlt_pipeline.yml sets bundle.sourcePath.
sys.path.append(SparkSession.getActiveSession().conf.get("bundle.sourcePath", "."))

from common.contracts import GOLD_SCHEMA  # noqa: E402
from common.identity import person_keys  # noqa: E402

MASK_FN = f"{GOLD_SCHEMA}.mask_name_or_mrn"
UNIT_ROW_FILTER = f"ROW FILTER {GOLD_SCHEMA}.unit_row_filter ON (unit)"


@dlt.table(
    name=f"{GOLD_SCHEMA}.dim_patients",
    comment="Gold: one row per patient, PHI-masked at the Unity Catalog layer.",
    schema=f"""
        patient_id STRING,
        event_ts TIMESTAMP,
        mrn STRING MASK {MASK_FN},
        full_name STRING MASK {MASK_FN},
        birth_date STRING,
        gender STRING,
        region STRING
    """,
)
def dim_patients():
    return dlt.read("silver_dim_patients")


# Data-steward decisions (agent/identity_review.py publish). Not a pipeline
# dataset: written by people through the review queue, read here as-is.
# Created empty by governance/05_unity_catalog/catalog_and_grants.sql
# (serverless blocks spark.catalog.tableExists, found on the first run).
IDENTITY_DECISIONS = "healthcare_agentic_de.ops.identity_decisions"


@dlt.table(
    name=f"{GOLD_SCHEMA}.dim_patient_person",
    comment=(
        "Gold: patient_id -> person_key. Ids a data steward confirmed as the same person (problem catalog "
        "M2, ops.identity_decisions) share one key, the smallest of their patient_ids; every other id is "
        "its own person. Count people with count(DISTINCT person_key). Ids only - no PHI. Shared ids "
        "(two people on one patient_id) aren't split here: that needs the admissions re-assigned at source."
    ),
)
def dim_patient_person():
    spark = SparkSession.getActiveSession()
    decided = spark.read.table(IDENTITY_DECISIONS).filter("decision = 'same_person'").select("patient_ids")
    pairs = [ids for ids in (r.patient_ids.split(",") for r in decided.collect()) if len(ids) == 2]
    keys = spark.createDataFrame(sorted(person_keys(pairs).items()), "patient_id string, person_key string")
    return (
        dlt.read("silver_dim_patients")
        .select("patient_id")
        .join(F.broadcast(keys), "patient_id", "left")
        .withColumn("person_key", F.coalesce("person_key", "patient_id"))
        .withColumn("steward_merged", F.col("person_key") != F.col("patient_id"))
    )


@dlt.table(
    name=f"{GOLD_SCHEMA}.fct_encounters",
    comment="Gold: current state of every encounter (admit/transfer/discharge).",
    row_filter=UNIT_ROW_FILTER,
)
def fct_encounters():
    return dlt.read("silver_fct_encounters")


@dlt.table(
    name=f"{GOLD_SCHEMA}.fct_encounter_history",
    comment=(
        "Gold: every version of every encounter, valid from valid_from until valid_to (NULL = current). "
        "Answers point-in-time questions such as the census of a unit at a given time."
    ),
    row_filter=UNIT_ROW_FILTER,
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
