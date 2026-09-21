"""Databricks-only DLT gold step. Not executed in this environment.

Gold materializes the Silver dimension/fact as-is for this domain — the
"business ready" transformation for encounters is really the apply_changes
logic already done in Silver, so Gold here is a thin passthrough that gives
BI/agent consumers a stable, documented table name and comment.
"""

import dlt


@dlt.table(name="dim_patients", comment="Gold: one row per patient, PHI-masked at the Unity Catalog layer.")
def dim_patients():
    return dlt.read("dim_patients")


@dlt.table(name="fct_encounters", comment="Gold: current state of every encounter (admit/transfer/discharge).")
def fct_encounters():
    return dlt.read("fct_encounters")
