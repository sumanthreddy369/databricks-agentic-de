"""Databricks-only DLT silver step. Not executed in this environment.

Encounters (and the patient dimension nested inside `admitted` events) are
MUTABLE STATE: a given encounter_id transitions admitted -> transferred* ->
discharged, and a given patient's demographic row can be corrected/updated.
That's exactly the CDC/upsert shape `dlt.apply_changes` (APPLY CHANGES INTO)
is built for — it's idempotent under replay and naturally expresses "the
latest known state of this key wins" via sequence_by. This is the opposite
write pattern from silver_vitals.py, which is an immutable, append-only time
series — see that file's docstring for the full contrast.

`known_event_type` is a hard-stop expectation (expect_or_fail): an event_type
outside the four the whole system understands is a genuine contract break in
the upstream feed, not a data-quality nuisance. It must fail the pipeline (and
therefore surface to the agent's check_expectation_metrics/check_job_status
tools as a failed job) so the orchestrator escalates via notify_and_page
rather than silently quarantining a symptom of a bigger problem.
"""

import dlt
from pyspark.sql import functions as F

from common.contracts import ALLOWED_EVENT_TYPES, ENCOUNTER_WATERMARK_MINUTES

_ALLOWED_TYPES_SQL = ", ".join(f"'{t}'" for t in ALLOWED_EVENT_TYPES)


@dlt.view(name="encounters_stream_view")
@dlt.expect_or_drop("valid_patient_id", "patient_id IS NOT NULL")
@dlt.expect_or_fail("known_event_type", f"event_type IN ({_ALLOWED_TYPES_SQL})")
def encounters_stream_view():
    bronze = dlt.read_stream("bronze_patient_events")
    return (
        bronze.filter(F.col("event_type").isin("admitted", "transferred", "discharged"))
        .withWatermark("event_ts", f"{ENCOUNTER_WATERMARK_MINUTES} minutes")
    )


dlt.create_streaming_table("fct_encounters")

dlt.apply_changes(
    target="fct_encounters",
    source="encounters_stream_view",
    keys=["encounter_id"],
    sequence_by="event_ts",
    apply_as_deletes=F.expr("event_type = 'discharged'"),
    except_column_list=["patient", "vital", "transfer", "notes", "_corrupt_record"],
    stored_as_scd_type=1,
)


dlt.create_streaming_table("dim_patients")

dlt.apply_changes(
    target="dim_patients",
    source="encounters_stream_view",
    keys=["patient_id"],
    sequence_by="event_ts",
    stored_as_scd_type=1,
    # dim_patients is only meaningfully populated on `admitted` events, where
    # the nested `patient` struct carries the demographic snapshot.
    except_column_list=["encounter", "vital", "transfer", "notes", "_corrupt_record", "encounter_id"],
)
