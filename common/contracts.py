"""Single source of truth for the `patient_events` domain model.

Every other file that touches the event schema — the DLT pipeline code under
`pipeline/`, the governance SQL under `governance/`, the simulator, the agent
tools, and the test suite — cross-checks against the constants defined here
instead of redefining them. If the schema changes, it changes here first.
"""

# One Kafka topic carries the full patient lifecycle (ADT: admit/transfer/
# discharge) plus vitals readings, mirroring a real hospital interface-engine
# pattern (e.g. HL7/FHIR event feeds) rather than splitting into many topics.
PATIENT_EVENT_ENVELOPE_FIELDS = [
    "event_id",
    "event_type",
    "event_ts",
    "patient_id",
    "encounter_id",
    "schema_version",
]

ALLOWED_EVENT_TYPES = ("admitted", "vitals_reading", "transferred", "discharged")

VITAL_ITEM_TYPES = ("heart_rate", "spo2", "resp_rate", "temp_c", "sbp", "dbp")

KAFKA_TOPIC_PATIENT_EVENTS = "patient_events"

# Vitals are an immutable, high-frequency time series -> short watermark.
VITALS_WATERMARK_MINUTES = 5

# Encounters (admit/transfer/discharge) are mutable state that can legitimately
# arrive with more lag (e.g. a delayed ADT feed) -> longer watermark.
ENCOUNTER_WATERMARK_MINUTES = 30

# Columns that must never reach an unprivileged reader (human or agent) in
# clear text. Enforced in two independent places: Unity Catalog column masks
# (governance/05_unity_catalog/row_filters_and_masking.sql) for direct SQL
# access, and agent/tools/governance_guard.py for anything the orchestrator
# agent reads and hands to an LLM.
MASKED_COLUMNS = {"dim_patients": {"full_name", "mrn"}}

# Illustrative vital-sign bands used ONLY to drive the synthetic simulator and
# to bound-check generated readings in Silver. These are rough clinical bands
# for realism, NOT sourced from real patient data, Synthea, or MIMIC-IV — do
# not treat them as clinically authoritative.
VITAL_RANGES = {
    "heart_rate": (55, 140),
    "spo2": (88, 100),
    "resp_rate": (10, 30),
    "temp_c": (35.5, 39.5),
    "sbp": (85, 160),
    "dbp": (50, 100),
}
