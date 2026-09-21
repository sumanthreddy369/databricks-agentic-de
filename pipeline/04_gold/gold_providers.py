"""Databricks-only DLT gold step. Not executed in this environment."""

import dlt


@dlt.table(name="dim_providers", comment="Gold: one row per care provider, joined against the roster feed.")
def dim_providers():
    return dlt.read("silver_provider_roster")
