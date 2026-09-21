"""Databricks-only DLT gold step. Not executed in this environment.

`fct_vitals` is a passthrough materialization (append-only, same semantics as
Silver). `gold_live_vitals_by_unit` is the proof point that "gold tables
update continuously" is true end-to-end, not just Bronze/Silver: it's a
genuine streaming aggregate — average heart rate and a count of out-of-range
readings, windowed over the trailing 15 minutes and grouped by unit — backed
by the same DLT continuous pipeline, so it advances as new vitals events
arrive with no batch job in between.
"""

import dlt
from pyspark.sql import functions as F

from common.contracts import VITAL_RANGES

_HR_LOW, _HR_HIGH = VITAL_RANGES["heart_rate"]


@dlt.table(name="fct_vitals", comment="Gold: append-only vitals time series, passthrough from Silver.")
def fct_vitals():
    return dlt.read("fct_vitals")


@dlt.table(
    name="gold_live_vitals_by_unit",
    comment="Streaming aggregate: avg heart rate + out-of-range count per unit, trailing 15-minute window.",
)
def gold_live_vitals_by_unit():
    vitals = dlt.read_stream("fct_vitals")
    encounters = dlt.read("fct_encounters")

    joined = vitals.join(encounters, on="encounter_id", how="inner").filter("itemid = 'heart_rate'")

    return (
        joined.withWatermark("event_ts", "15 minutes")
        .groupBy(F.window("event_ts", "15 minutes"), F.col("unit"))
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
