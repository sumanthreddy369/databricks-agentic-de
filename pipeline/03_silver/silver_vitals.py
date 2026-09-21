"""Databricks-only DLT silver step. Not executed in this environment.

Vitals readings are an IMMUTABLE FACT: a heart-rate reading taken at a given
instant never changes after the fact, there is nothing to "upsert" — a new
reading is a new row, forever. That's why this table deliberately does NOT
use `dlt.apply_changes` (which models mutable dimension/state keyed by a
business key, e.g. silver_encounters.py's dim_patients/fct_encounters).
Instead it uses the append-only streaming pattern: `withWatermark` bounds how
long we wait for a late reading before considering a window closed, and
`dropDuplicatesWithinWatermark` gives exactly-once-style semantics against
Kafka at-least-once redelivery, keyed on the event's own UUID rather than any
business key that would imply "latest wins."

`plausible_vital_value` is a hard drop (expect_or_drop): a physically
impossible reading (e.g. negative heart rate) is safe to quarantine silently,
it doesn't indicate a broken upstream contract the way an unknown event_type
does. `known_vital_item` is warn-only (expect) — an unrecognized itemid is
worth surfacing in pipeline health metrics but not worth losing the row over.
"""

import dlt

from common.contracts import VITAL_ITEM_TYPES, VITALS_WATERMARK_MINUTES

_KNOWN_ITEMS_SQL = ", ".join(f"'{v}'" for v in VITAL_ITEM_TYPES)


@dlt.table(
    name="fct_vitals",
    comment="Append-only vitals time series. Never upserted — see module docstring.",
)
@dlt.expect_or_drop("plausible_vital_value", "vital.value BETWEEN 0 AND 300")
@dlt.expect("known_vital_item", f"vital.itemid IN ({_KNOWN_ITEMS_SQL})")
def fct_vitals():
    bronze = dlt.read_stream("bronze_patient_events")
    return (
        bronze.filter("event_type = 'vitals_reading'")
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
