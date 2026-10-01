"""Databricks-only DLT silver step. Not executed in this environment.

Encounters (and the patient dimension nested inside `admitted` events) are
MUTABLE STATE: a given encounter_id transitions admitted -> transferred* ->
discharged, and a given patient's demographic row can be corrected/updated.
That's exactly the CDC/upsert shape `dlt.apply_changes` (APPLY CHANGES INTO)
is built for — it's idempotent under replay and naturally expresses "the
latest known state of this key wins" via sequence_by. This is the opposite
write pattern from silver_vitals.py, which is an immutable, append-only time
series — see that file's docstring for the full contrast.

Why each CDC source is a flattening view rather than the raw event row:
each event type carries a different slice of the state. `admitted` has the
nested `patient`/`encounter` structs, `transferred` only has
`transfer.to_unit`, `discharged` has neither. Upserting the raw rows under
SCD type 1 would overwrite a patient's demographics (and an encounter's unit)
with NULL on every transfer/discharge. So each view projects its event types
onto the flat target columns (the same columns Gold, the Unity Catalog masks
on `full_name`/`mrn`, the `unit` row filter, and the local DuckDB seed all
expect), and `ignore_null_updates=True` means a column an event doesn't carry
keeps its previous value.

A discharge sets `status = 'discharged'` rather than deleting the encounter,
so "how many patients were discharged today" stays answerable from Gold.

The `known_event_type` hard stop lives upstream in silver_patient_events.py;
both views here read its output, never Bronze directly.
"""

import sys

import dlt
from pyspark.sql import SparkSession
from pyspark.sql import functions as F

# DLT doesn't put the bundle root on sys.path, so project imports need it
# added explicitly; resources/dlt_pipeline.yml sets bundle.sourcePath.
sys.path.append(SparkSession.getActiveSession().conf.get("bundle.sourcePath", "."))

from common.contracts import ENCOUNTER_WATERMARK_MINUTES  # noqa: E402


@dlt.view(name="encounters_cdc_view")
@dlt.expect_or_drop("valid_patient_id", "patient_id IS NOT NULL")
@dlt.expect_or_drop("valid_encounter_id", "encounter_id IS NOT NULL")
def encounters_cdc_view():
    events = dlt.read_stream("silver_contract_checked_events")
    return (
        events.filter(F.col("event_type").isin("admitted", "transferred", "discharged"))
        .withWatermark("event_ts", f"{ENCOUNTER_WATERMARK_MINUTES} minutes")
        .select(
            "encounter_id",
            "patient_id",
            "event_ts",
            "event_type",
            F.col("encounter.encounter_type").alias("encounter_type"),
            # admitted -> the admitting unit; transferred -> the new unit;
            # discharged -> NULL, so ignore_null_updates keeps the last unit.
            F.coalesce(F.col("transfer.to_unit"), F.col("encounter.unit")).alias("unit"),
            F.col("encounter.attending_provider_id").alias("attending_provider_id"),
            F.when(F.col("event_type") == "discharged", F.lit("discharged"))
            .otherwise(F.col("encounter.status"))
            .alias("status"),
        )
    )


dlt.create_streaming_table("silver_fct_encounters")

dlt.apply_changes(
    target="silver_fct_encounters",
    source="encounters_cdc_view",
    keys=["encounter_id"],
    sequence_by="event_ts",
    ignore_null_updates=True,
    except_column_list=["event_type"],
    stored_as_scd_type=1,
)

# Same source, kept as history (SCD type 2): one row per version of each
# encounter with __START_AT/__END_AT validity bounds. silver_fct_encounters
# only knows where a patient is NOW; this table knows where they were at any
# moment, which is what attributing a vitals reading to the unit it was taken
# in (gold_live_vitals_by_unit) and "as of 3am" questions
# (gold.fct_encounter_history) both need.
dlt.create_streaming_table("silver_fct_encounter_history")

dlt.apply_changes(
    target="silver_fct_encounter_history",
    source="encounters_cdc_view",
    keys=["encounter_id"],
    sequence_by="event_ts",
    ignore_null_updates=True,
    except_column_list=["event_type"],
    stored_as_scd_type=2,
)


@dlt.view(name="patients_cdc_view")
@dlt.expect_or_drop("valid_patient_id", "patient_id IS NOT NULL")
def patients_cdc_view():
    # Only `admitted` events carry the demographic snapshot.
    events = dlt.read_stream("silver_contract_checked_events")
    return events.filter("event_type = 'admitted' AND patient IS NOT NULL").select(
        "patient_id",
        "event_ts",
        F.col("patient.mrn").alias("mrn"),
        F.col("patient.full_name").alias("full_name"),
        F.col("patient.birth_date").alias("birth_date"),
        F.col("patient.gender").alias("gender"),
        F.col("patient.region").alias("region"),
    )


dlt.create_streaming_table("silver_dim_patients")

dlt.apply_changes(
    target="silver_dim_patients",
    source="patients_cdc_view",
    keys=["patient_id"],
    sequence_by="event_ts",
    ignore_null_updates=True,
    stored_as_scd_type=1,
)
