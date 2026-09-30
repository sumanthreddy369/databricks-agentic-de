"""Single source of truth for the `patient_events` domain model.

Every other file that touches the event schema — the DLT pipeline code under
`pipeline/`, the governance SQL under `governance/`, the simulator, the agent
tools, and the test suite — cross-checks against the constants defined here
instead of redefining them. If the schema changes, it changes here first.

The Pydantic models below (`PatientEvent` and its nested pieces) give the
same envelope a real, importable runtime schema: malformed events (wrong
`event_type`, wrong `itemid`, wrong field types) fail construction instead of
silently propagating, which is the guardrail-relevant reason these exist —
see docs/architecture.md's "Guardrails" section (input validation). They are
additive, parallel to `simulator/domain.py`'s plain dataclasses (kept
unchanged there for the simulator's own reasons) and to
`pipeline/common/schemas.py`'s PySpark StructTypes (Databricks-only) — all
three describe the same wire shape from different angles, and all three
cross-check against the constants in this file.
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, model_validator

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

# Unity Catalog schema the agent-facing Gold tables are published to. The
# `orchestrator_agent` group's only SELECT grant is on this schema
# (governance/05_unity_catalog/catalog_and_grants.sql), so the DLT gold step
# (pipeline/04_gold/) and the live DA-mode query path must both use it.
GOLD_SCHEMA = "healthcare_agentic_de.gold"

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

# --- Guardrail size limits -------------------------------------------------
# Shared caps enforced by agent/orchestrator.py and agent/tools/governance_guard.py.
# Defined here (not locally in either file) for the same reason as everything
# else in this module: one source of truth, cross-checked by tests.

# Input-size guardrail: caps how much free-text `notes` content is scanned by
# governance_guard.scan_for_injection in one call — a defense against
# extremely long adversarial payloads designed to bury or dilute an
# injection attempt, or simply to waste scan time.
MAX_NOTES_LENGTH = 2000

# Output-size guardrail: caps how many rows from a query_gold_table result are
# ever placed into a ToolResult.content (and therefore into the Claude
# message history) in one call.
MAX_TOOL_RESULT_ROWS = 500

# Output-size guardrail: a generic character cap applied to ANY tool result's
# content (not just row-shaped query_gold_table results) before it is
# appended to `messages`, so no single tool call can blow up the transcript.
MAX_TOOL_RESULT_CHARS = 20_000


# --- Pydantic envelope models -----------------------------------------------
# Built from the constants above so the two representations (constants +
# models) cannot drift apart — see tests/test_contract_consistency.py and
# tests/test_contracts_pydantic.py.

EventType = Enum("EventType", {name.upper(): name for name in ALLOWED_EVENT_TYPES}, type=str)
VitalItemId = Enum("VitalItemId", {name.upper(): name for name in VITAL_ITEM_TYPES}, type=str)


class _StrictModel(BaseModel):
    """Shared base: reject unknown fields rather than silently ignore them,
    since an unexpected field on a healthcare event envelope is exactly the
    kind of schema-drift signal `agent/tools/pipeline_health.detect_schema_drift`
    is meant to catch — the Pydantic layer should behave the same way.
    """

    model_config = ConfigDict(extra="forbid")


class PatientInfo(_StrictModel):
    mrn: str
    full_name: str
    birth_date: str
    gender: str
    region: str


class EncounterInfo(_StrictModel):
    encounter_type: str
    unit: str
    attending_provider_id: str
    status: str


class TransferInfo(_StrictModel):
    from_unit: str
    to_unit: str


class VitalReading(_StrictModel):
    """The envelope's nested `vital` object.

    `value` is range-checked against `VITAL_RANGES[itemid]` in
    `_flag_out_of_range` below, but an out-of-range value is FLAGGED
    (`out_of_range=True`) rather than rejected — mirroring the DLT
    `dlt.expect` (log-and-keep) vs. `expect_or_drop`/`expect_or_fail`
    (reject) distinction used in `pipeline/03_silver/silver_vitals.py` and
    `silver_encounters.py`. `simulator/domain.py` deliberately produces a
    small out-of-range tail (`OUT_OF_RANGE_PROBABILITY`) for realism —
    real vitals monitors do occasionally report implausible readings — so
    construction must not fail on those readings; only a hard structural
    problem (wrong type, unknown itemid) should.
    """

    itemid: VitalItemId
    value: float
    valueuom: str
    out_of_range: bool = False

    @model_validator(mode="after")
    def _flag_out_of_range(self) -> "VitalReading":
        low, high = VITAL_RANGES[self.itemid]
        self.out_of_range = not (low <= self.value <= high)
        return self


class PatientEvent(_StrictModel):
    """The full `patient_events` wire envelope. See this module's docstring
    and README.md's "Domain: patient_events" section for the JSON shape.
    """

    event_id: str
    event_type: EventType
    event_ts: datetime
    patient_id: str
    encounter_id: str
    schema_version: int = 1
    patient: PatientInfo | None = None
    encounter: EncounterInfo | None = None
    transfer: TransferInfo | None = None
    vital: VitalReading | None = None
    notes: str | None = None
