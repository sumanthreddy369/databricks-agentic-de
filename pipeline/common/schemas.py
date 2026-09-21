"""PySpark StructTypes for the `patient_events` Kafka payload and the
provider roster feed, built from the same constants in `common.contracts` so
the two never drift apart (see `tests/test_contract_consistency.py`).

Databricks-only: this module is imported by the `pipeline/` DLT notebooks/
files, which only run on a real Databricks cluster with `pyspark`/`dlt`
available. It is intentionally NOT imported by `agent/`, `simulator/`, or
`common/` so that the local test suite never needs pyspark installed.

Envelope fields (kept in sync with common.contracts.PATIENT_EVENT_ENVELOPE_FIELDS):
    event_id, event_type, event_ts, patient_id, encounter_id, schema_version
plus the conditionally-populated nested structs: patient, encounter, transfer,
vital, and the free-text notes field.
"""

from pyspark.sql.types import (
    DoubleType,
    StringType,
    StructField,
    StructType,
    TimestampType,
)

PATIENT_STRUCT = StructType(
    [
        StructField("mrn", StringType(), True),
        StructField("full_name", StringType(), True),
        StructField("birth_date", StringType(), True),
        StructField("gender", StringType(), True),
        StructField("region", StringType(), True),
    ]
)

ENCOUNTER_STRUCT = StructType(
    [
        StructField("encounter_type", StringType(), True),
        StructField("unit", StringType(), True),
        StructField("attending_provider_id", StringType(), True),
        StructField("status", StringType(), True),
    ]
)

TRANSFER_STRUCT = StructType(
    [
        StructField("from_unit", StringType(), True),
        StructField("to_unit", StringType(), True),
    ]
)

VITAL_STRUCT = StructType(
    [
        StructField("itemid", StringType(), True),
        StructField("value", DoubleType(), True),
        StructField("valueuom", StringType(), True),
    ]
)

# Mirrors common.contracts.PATIENT_EVENT_ENVELOPE_FIELDS plus the nested
# structs and the free-text notes field (the deliberate prompt-injection
# attack surface — see governance/05_unity_catalog and agent/tools/governance_guard.py).
PATIENT_EVENT_SCHEMA = StructType(
    [
        StructField("event_id", StringType(), True),
        StructField("event_type", StringType(), True),
        StructField("event_ts", TimestampType(), True),
        StructField("patient_id", StringType(), True),
        StructField("encounter_id", StringType(), True),
        StructField("schema_version", StringType(), True),
        StructField("patient", PATIENT_STRUCT, True),
        StructField("encounter", ENCOUNTER_STRUCT, True),
        StructField("transfer", TRANSFER_STRUCT, True),
        StructField("vital", VITAL_STRUCT, True),
        StructField("notes", StringType(), True),
    ]
)

PROVIDER_ROSTER_SCHEMA = StructType(
    [
        StructField("provider_id", StringType(), True),
        StructField("full_name", StringType(), True),
        StructField("specialty", StringType(), True),
        StructField("npi", StringType(), True),
        StructField("home_unit", StringType(), True),
        StructField("updated_at", TimestampType(), True),
    ]
)
