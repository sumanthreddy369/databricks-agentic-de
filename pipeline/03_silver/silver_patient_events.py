"""Databricks-only DLT silver step. Not executed in this environment.

The single contract gate every Silver consumer of `bronze_patient_events`
reads through (silver_encounters.py, silver_vitals.py). It does no
filtering of its own, on purpose.

`known_event_type` is a hard-stop expectation (expect_or_fail): an event_type
outside the four the whole system understands is a genuine contract break in
the upstream feed, not a data-quality nuisance. It must fail the pipeline (and
therefore surface to the agent's check_expectation_metrics/check_job_status
tools as a failed update) so the orchestrator escalates via notify_and_page
rather than silently quarantining a symptom of a bigger problem. For that to
be possible it has to see every event: a consumer that filtered by
event_type first (e.g. `isin("admitted", ...)`) would remove an unknown type
before the expectation ever evaluated it, and the hard stop could never
fire. tests/test_dlt_pipeline_graph.py asserts this view reads Bronze
unfiltered and is the only Silver dataset that reads Bronze at all.

A row with a NULL event_type (PERMISSIVE-mode JSON parsing routed it to
`_corrupt_record` at ingest) is malformed, not an unknown type, so it is
dropped by `has_event_type` rather than failing the pipeline.
"""

import sys

import dlt
from pyspark.sql import SparkSession

# DLT doesn't put the bundle root on sys.path, so project imports need it
# added explicitly; resources/dlt_pipeline.yml sets bundle.sourcePath.
sys.path.append(SparkSession.getActiveSession().conf.get("bundle.sourcePath", "."))

from common.contracts import ALLOWED_EVENT_TYPES  # noqa: E402

_ALLOWED_TYPES_SQL = ", ".join(f"'{t}'" for t in ALLOWED_EVENT_TYPES)


@dlt.view(
    name="silver_contract_checked_events",
    comment="Every Bronze patient event, gated by the known_event_type hard stop. Never filtered by type.",
)
@dlt.expect_or_drop("has_event_type", "event_type IS NOT NULL")
@dlt.expect_or_fail("known_event_type", f"event_type IS NULL OR event_type IN ({_ALLOWED_TYPES_SQL})")
def silver_contract_checked_events():
    return dlt.read_stream("bronze_patient_events")
