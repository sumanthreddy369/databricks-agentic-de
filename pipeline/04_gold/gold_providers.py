"""Databricks-only DLT gold step. Not executed in this environment.

Published into `common.contracts.GOLD_SCHEMA` by fully-qualified name — see
gold_encounters.py's docstring for why.
"""

import sys

import dlt
from pyspark.sql import SparkSession

# DLT doesn't put the bundle root on sys.path, so project imports need it
# added explicitly; resources/dlt_pipeline.yml sets bundle.sourcePath.
sys.path.append(SparkSession.getActiveSession().conf.get("bundle.sourcePath", "."))

from common.contracts import GOLD_SCHEMA  # noqa: E402


@dlt.table(name=f"{GOLD_SCHEMA}.dim_providers", comment="Gold: one row per care provider, from the roster feed.")
def dim_providers():
    return dlt.read("silver_provider_roster")
