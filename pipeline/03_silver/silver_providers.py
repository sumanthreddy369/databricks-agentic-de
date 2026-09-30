"""Databricks-only DLT silver step. Not executed in this environment.

The provider roster is small reference data with a natural key
(provider_id). Each Autoloader batch file is a snapshot of changed rows, so
the same provider appears once per batch they were touched in — appending
those rows straight through would give Gold's "one row per provider"
dim_providers duplicates. `apply_changes` keyed on provider_id, sequenced by
the roster's own `updated_at`, keeps exactly the latest row per provider.
"""

import dlt


@dlt.view(name="provider_roster_cdc_view")
@dlt.expect_or_drop("valid_provider_id", "provider_id IS NOT NULL")
def provider_roster_cdc_view():
    return dlt.read_stream("bronze_provider_roster")


dlt.create_streaming_table("silver_provider_roster")

dlt.apply_changes(
    target="silver_provider_roster",
    source="provider_roster_cdc_view",
    keys=["provider_id"],
    sequence_by="updated_at",
    except_column_list=["_ingested_at"],
    stored_as_scd_type=1,
)
