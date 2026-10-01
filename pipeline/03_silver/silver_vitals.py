"""Databricks-only DLT silver step. Not executed in this environment.

Vitals readings are an IMMUTABLE FACT: a heart-rate reading taken at a given
instant never changes after the fact, there is nothing to "upsert" — a new
reading is a new row, forever. That's why this table deliberately does NOT
use `dlt.apply_changes` (which models mutable dimension/state keyed by a
business key, e.g. silver_encounters.py's silver_dim_patients/silver_fct_encounters).
Instead it uses the append-only streaming pattern: `withWatermark` bounds how
long we wait for a late reading before considering a window closed, and
`dropDuplicatesWithinWatermark` gives exactly-once-style semantics against
Kafka at-least-once redelivery, keyed on the event's own UUID rather than any
business key that would imply "latest wins."

Reads through silver_patient_events.py's contract gate, never Bronze
directly, so an unknown event_type fails the pipeline before any vitals are
written.

`plausible_vital_value` is a hard drop (expect_or_drop): a physically
impossible reading is safe to quarantine silently. "Impossible" is per vital
(common.contracts.PHYSIOLOGIC_LIMITS): it excludes the 0 many devices send
when disconnected, SpO2 above 100%, and Fahrenheit sent as temp_c, while
keeping clinical alarms like a heart rate of 145. (It used to be a single
`value BETWEEN 0 AND 300`, which let a disconnected sensor's 0 through as a
real reading.) Dropping is right because an impossible reading doesn't
indicate a broken upstream contract the way an unknown event_type
does. `known_vital_item` is warn-only (expect) — an unrecognized itemid is
worth surfacing in pipeline health metrics but not worth losing the row over.
"""

import sys

import dlt
from pyspark.sql import SparkSession

# DLT doesn't put the bundle root on sys.path, so project imports need it
# added explicitly; resources/dlt_pipeline.yml sets bundle.sourcePath.
sys.path.append(SparkSession.getActiveSession().conf.get("bundle.sourcePath", "."))

from common.contracts import VITAL_ITEM_TYPES, VITALS_WATERMARK_MINUTES, plausible_vital_sql  # noqa: E402

_KNOWN_ITEMS_SQL = ", ".join(f"'{v}'" for v in VITAL_ITEM_TYPES)


@dlt.table(
    name="silver_fct_vitals",
    comment="Append-only vitals time series. Never upserted — see module docstring.",
)
# Expectations evaluate against this function's OUTPUT, which has the flattened
# `value`/`itemid` columns below, not the Bronze `vital` struct.
@dlt.expect_or_drop("plausible_vital_value", plausible_vital_sql())
@dlt.expect("known_vital_item", f"itemid IN ({_KNOWN_ITEMS_SQL})")
def silver_fct_vitals():
    events = dlt.read_stream("silver_contract_checked_events")
    return (
        events.filter("event_type = 'vitals_reading'")
        .withWatermark("event_ts", f"{VITALS_WATERMARK_MINUTES} minutes")
        .dropDuplicatesWithinWatermark(["event_id"])
        .selectExpr(
            "event_id",
            "event_ts",
            "patient_id",
            "encounter_id",
            "vital.itemid AS itemid",
            "vital.value AS value",
            "vital.valueuom AS valueuom",
        )
    )
