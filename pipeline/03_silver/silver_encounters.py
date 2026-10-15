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


@dlt.table(
    name="silver_patient_admissions",
    comment="Append-only: one row per admission with the demographic snapshot it carried (source for M2 checks).",
)
def silver_patient_admissions():
    events = dlt.read_stream("silver_contract_checked_events")
    return events.filter("event_type = 'admitted' AND patient IS NOT NULL").select(
        "patient_id",
        "encounter_id",
        "event_ts",
        F.col("patient.full_name").alias("full_name"),
        F.col("patient.birth_date").alias("birth_date"),
        F.col("patient.gender").alias("gender"),
    )


@dlt.table(
    name="silver_patient_identity_conflicts",
    comment=(
        "Data-quality check (problem catalog M2): patient_ids admitted more than once with a different "
        "name or birth date - two people on one id. Counts only - no names. A data steward decides; "
        "records are never merged automatically."
    ),
)
@dlt.expect("one_identity_per_patient_id", "names = 1 AND birth_dates = 1")
def silver_patient_identity_conflicts():
    # Batch read of the admissions table: an aggregate over all of a patient's
    # admissions, downstream of the contract gate.
    return (
        dlt.read("silver_patient_admissions")
        .groupBy("patient_id")
        .agg(
            F.countDistinct("encounter_id").alias("admissions"),
            F.countDistinct("full_name").alias("names"),
            F.countDistinct("birth_date").alias("birth_dates"),
            F.countDistinct("gender").alias("genders"),
            F.min("event_ts").alias("first_admitted"),
            F.max("event_ts").alias("last_admitted"),
        )
        .filter("admissions > 1")
    )


@dlt.table(
    name="silver_patient_duplicate_candidates",
    comment=(
        "Data-quality check (problem catalog M2): one person under more than one patient_id - admissions "
        "sharing a name and birth date across patient_ids. The match key is a hash; no names. Candidates "
        "only: exact name + birth date can also match two different people, so a data steward decides."
    ),
)
@dlt.expect("one_patient_id_per_person", "patient_ids = 1")
def silver_patient_duplicate_candidates():
    return (
        dlt.read("silver_patient_admissions")
        .groupBy(F.sha2(F.concat_ws("|", "full_name", "birth_date"), 256).alias("match_key"))
        .agg(
            F.countDistinct("patient_id").alias("patient_ids"),
            F.array_sort(F.collect_set("patient_id")).alias("candidate_patient_ids"),
            F.count("*").alias("admissions"),
        )
        .filter("patient_ids > 1")
    )


@dlt.table(
    name="silver_patient_fuzzy_duplicate_candidates",
    comment=(
        "Data-quality check (problem catalog M2): one person under two patient_ids whose registrations differ "
        "slightly - a name within 2 edits (typo, transposition) with the same birth date, or the same name "
        "with day and month swapped. Exact matches are in silver_patient_duplicate_candidates. Pairs of "
        "patient_ids only, no names; candidates for a data steward, never merged automatically."
    ),
)
@dlt.expect("no_near_duplicate_registration", "name_edit_distance > 2")
def silver_patient_fuzzy_duplicate_candidates():
    people = (
        dlt.read("silver_patient_admissions")
        .select("patient_id", F.lower("full_name").alias("name"), "birth_date", "gender")
        .dropDuplicates()
        # Blocking key: gender, birth year and the unordered day/month pair, so a
        # swapped day and month still lands in the same block (an equi-join).
        .withColumn(
            "block",
            F.concat_ws(
                "|",
                "gender",
                F.substring("birth_date", 1, 4),
                F.array_join(
                    F.array_sort(F.array(F.substring("birth_date", 6, 2), F.substring("birth_date", 9, 2))), "-"
                ),
            ),
        )
    )
    a, b = people.alias("a"), people.alias("b")
    pairs = a.join(b, (F.col("a.block") == F.col("b.block")) & (F.col("a.patient_id") < F.col("b.patient_id")))
    distance = F.levenshtein(F.col("a.name"), F.col("b.name"))
    same_date = F.col("a.birth_date") == F.col("b.birth_date")
    return (
        pairs.select(
            F.col("a.patient_id").alias("patient_id_a"),
            F.col("b.patient_id").alias("patient_id_b"),
            distance.alias("name_edit_distance"),
            F.when(same_date, F.lit("same")).otherwise(F.lit("day and month swapped")).alias("birth_date_match"),
        )
        # Near, not exact: exact name + birth date pairs are the other check's.
        .filter("name_edit_distance <= 2 AND NOT (name_edit_distance = 0 AND birth_date_match = 'same')")
    )
