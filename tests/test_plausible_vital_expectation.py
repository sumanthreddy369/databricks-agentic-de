"""Evaluates the exact SQL expression Silver's `plausible_vital_value`
expectation uses (common.contracts.plausible_vital_sql) in DuckDB, so the
keep/drop decision for real-world edge cases is proven locally rather than
only on a Databricks cluster.
"""

import duckdb
import pytest

from common.contracts import VITAL_RANGES, plausible_vital_sql


def _passes(itemid, value) -> bool:
    conn = duckdb.connect()
    try:
        row = conn.execute(
            f"SELECT {plausible_vital_sql()} FROM (SELECT ?::VARCHAR AS itemid, ?::DOUBLE AS value)",
            [itemid, value],
        ).fetchone()
    finally:
        conn.close()
    return bool(row[0])


@pytest.mark.parametrize(
    ("itemid", "value", "why"),
    [
        ("heart_rate", 0, "disconnected-device sentinel"),
        ("spo2", 0, "disconnected-device sentinel"),
        ("heart_rate", -1, "sentinel"),
        ("heart_rate", 999, "sentinel"),
        ("spo2", 106, "saturation can't exceed 100%"),
        ("temp_c", 98.6, "Fahrenheit sent as Celsius"),
        ("heart_rate", None, "missing value"),
    ],
)
def test_impossible_readings_are_dropped(itemid, value, why):
    assert not _passes(itemid, value), why


@pytest.mark.parametrize(
    ("itemid", "value"),
    [("heart_rate", 145), ("heart_rate", 40), ("spo2", 82), ("temp_c", 41.2), ("sbp", 210), ("resp_rate", 38)],
)
def test_clinical_alarms_are_kept(itemid, value):
    # Out of the normal band (an alarm a clinician needs to see), but real.
    assert _passes(itemid, value)


def test_every_normal_band_is_inside_the_physical_limits():
    for itemid, (low, high) in VITAL_RANGES.items():
        assert _passes(itemid, low) and _passes(itemid, high), itemid


def test_unknown_item_is_kept_for_the_warn_only_expectation():
    assert _passes("pain_score", 5)
