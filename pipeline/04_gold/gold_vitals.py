"""Lakeflow (DLT) gold step. Ran successfully on the GCP trial workspace.

`fct_vitals` is a streaming passthrough of Silver's append-only vitals (read
with `read_stream`, so each update only processes new readings instead of
recomputing the whole time series).

`gold_live_vitals_by_unit` - average heart rate and out-of-range count per
unit in 15-minute event-time windows - is a materialized view, recomputed on
every pipeline update (continuously, on a continuous pipeline). It used to be
a streaming append-mode aggregate. The first real run showed why that's
wrong for a "live" table: a streaming window is only emitted once the
watermark passes its end, so the current window - the one clinicians care
about - never appears, and a 10-minute batch produced zero rows. As a
materialized view, closed windows are exact and the latest window shows what
has arrived so far; readers must treat the newest window as still filling
(problem catalog O5).

Both are published into `common.contracts.GOLD_SCHEMA` by fully-qualified
name — see gold_encounters.py's docstring for why.
"""

import sys

import dlt
from pyspark.sql import SparkSession
from pyspark.sql import functions as F

# DLT doesn't put the bundle root on sys.path, so project imports need it
# added explicitly; resources/dlt_pipeline.yml sets bundle.sourcePath.
sys.path.append(SparkSession.getActiveSession().conf.get("bundle.sourcePath", "."))

from common.contracts import GOLD_SCHEMA, VITAL_RANGES  # noqa: E402

_HR_LOW, _HR_HIGH = VITAL_RANGES["heart_rate"]

# Readings with no encounter version in effect at their event time.
UNASSIGNED_UNIT = "UNASSIGNED"


@dlt.table(
    name=f"{GOLD_SCHEMA}.fct_vitals",
    comment="Gold: append-only vitals time series, passthrough from Silver.",
)
def fct_vitals():
    return dlt.read_stream("silver_fct_vitals")


@dlt.table(
    name=f"{GOLD_SCHEMA}.gold_live_vitals_by_unit",
    comment=(
        "Avg heart rate + out-of-range count per unit per 15-minute event-time window. "
        "The newest window may still be filling."
    ),
)
def gold_live_vitals_by_unit():
    # Each reading is attributed to the unit the patient was in WHEN IT WAS
    # TAKEN: an event-time range join against the encounter history, not an
    # equality join against current state (which would move a reading taken
    # in the ED into the ICU once the patient is transferred). Left join, so
    # a reading that arrives before its admit event lands in UNASSIGNED
    # instead of silently disappearing from the aggregate.
    vitals = dlt.read("silver_fct_vitals").filter("itemid = 'heart_rate'").alias("v")
    history = dlt.read("silver_fct_encounter_history").select(
        "encounter_id", "unit", F.col("__START_AT").alias("valid_from"), F.col("__END_AT").alias("valid_to")
    ).alias("h")
    in_effect_at_reading = (
        (F.col("v.encounter_id") == F.col("h.encounter_id"))
        & (F.col("v.event_ts") >= F.col("h.valid_from"))
        & (F.col("h.valid_to").isNull() | (F.col("v.event_ts") < F.col("h.valid_to")))
    )
    joined = vitals.join(history, on=in_effect_at_reading, how="left").select(
        F.col("v.event_ts").alias("event_ts"),
        F.col("v.value").alias("value"),
        F.coalesce(F.col("h.unit"), F.lit(UNASSIGNED_UNIT)).alias("unit"),
    )
    return (
        joined.groupBy(F.window("event_ts", "15 minutes"), F.col("unit"))
        .agg(
            F.avg("value").alias("avg_heart_rate"),
            F.sum(F.when((F.col("value") < _HR_LOW) | (F.col("value") > _HR_HIGH), 1).otherwise(0)).alias(
                "out_of_range_count"
            ),
            F.count("*").alias("reading_count"),
        )
        .select(
            F.col("window.start").alias("window_start"),
            F.col("window.end").alias("window_end"),
            "unit",
            "avg_heart_rate",
            "out_of_range_count",
            "reading_count",
        )
    )
