"""Databricks-only DLT silver step. Not executed in this environment.

The provider roster is small reference data with a natural key
(provider_id) — a straightforward drop-invalid-rows table, no apply_changes
needed here since gold_providers.py handles the dimension materialization.
"""

import dlt


@dlt.table(
    name="silver_provider_roster",
    comment="Provider roster rows with a valid provider_id.",
)
@dlt.expect_or_drop("valid_provider_id", "provider_id IS NOT NULL")
def silver_provider_roster():
    return dlt.read_stream("bronze_provider_roster")
