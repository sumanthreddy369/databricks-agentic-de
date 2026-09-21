# Databricks Agentic DE

Project 2 of a two-part portfolio series (Snowflake + Databricks — Project 1
is [`snowflake-ai-data-agent`](../snowflake-ai-data-agent), bank-card
transactions, backed by Snowflake's no-code Cortex Analyst). This project
takes the harder, more differentiated path: **healthcare, real-time
patient/vitals monitoring**, with **one hand-built orchestrator agent** —
not a vendor no-code tool — that does two jobs a real DE/DA team would
otherwise split across people:

1. **Keeps the streaming pipeline itself healthy.** Detects schema drift,
   failing data-quality expectations, and stuck jobs, and auto-remediates
   the auto-fixable cases with no human data engineer paged in — while
   correctly escalating genuine contract breaks instead of silently masking
   them.
2. **Answers clinical/ops questions in plain English** over governed Gold
   tables, with PHI masking and prompt-injection defense that are
   *architecturally* enforced and *tested*, not just claimed in a system
   prompt — see [`docs/architecture.md`](docs/architecture.md) and
   `tests/test_masking_guard.py` / `tests/test_prompt_injection_guard.py`.

## Pipeline

| Step | What happens | Tool | Where |
|---|---|---|---|
| 1. Ingest | Kafka stream (ADT + vitals) + Autoloader (provider roster) | Kafka (Redpanda locally) / Databricks Autoloader | [`pipeline/01_ingest/`](pipeline/01_ingest/) |
| 2. Bronze | Raw payloads flattened, nothing dropped | Delta Live Tables | [`pipeline/02_bronze/`](pipeline/02_bronze/) |
| 3. Silver | Two write patterns: CDC upsert for state, append+watermark for the vitals time series | DLT `apply_changes` / `withWatermark` | [`pipeline/03_silver/`](pipeline/03_silver/) |
| 4. Gold | Business-ready dims/facts + a continuously-updating streaming aggregate | Delta Live Tables | [`pipeline/04_gold/`](pipeline/04_gold/) |
| 5. Governance | Column masking + unit-scoped row filters | Unity Catalog | [`governance/05_unity_catalog/`](governance/05_unity_catalog/) |
| 6. Agent | DE-mode pipeline self-healing + DA-mode Q&A, one orchestrator | Hand-built Claude tool-loop agent | [`agent/`](agent/) |
| 7. Orchestrate | Two DLT pipelines, one hourly reference-data job | Databricks Asset Bundle | [`databricks.yml`](databricks.yml), [`resources/`](resources/) |
| 8. Validate | Mocked-LLM pytest suite proving masking/injection-defense/remediation | pytest | [`tests/`](tests/) |

## Layout

```
databricks-agentic-de/
├── pipeline/                # Databricks-only: DLT bronze/silver/gold, structurally complete, not run here
│   ├── common/schemas.py
│   ├── 01_ingest/  02_bronze/  03_silver/  04_gold/
├── governance/05_unity_catalog/   # Unity Catalog grants, column masks, row filters (Databricks-only)
├── common/contracts.py       # single source of truth: event schema, allowed types, masked columns
├── agent/                    # the real, locally-runnable deliverable
│   ├── llm.py                # thin Claude tool-loop wrapper, no agent framework
│   ├── orchestrator.py       # OrchestratorAgent: DE/DA routing + guardrail wiring
│   ├── prompts.py  state.py
│   └── tools/{pipeline_health.py, data_query.py, governance_guard.py}
├── simulator/                # streaming patient-event + provider-roster generators
│   ├── domain.py  population.py  producer.py  autoloader_feed.py  chaos.py
├── data/
│   ├── reference/synthea_sample/   # reference artifact only — not read at runtime
│   ├── seed/gold_seed.sql
│   └── state/pipeline_state.example.json
├── tests/                    # 9 files, mocked Anthropic client, zero network calls
└── docs/architecture.md
```

## Domain: `patient_events`

One Kafka topic carries the whole patient lifecycle plus vitals (a realistic
hospital interface-engine pattern), `event_type ∈ {admitted, vitals_reading,
transferred, discharged}`:

```json
{
  "event_id": "uuid", "event_type": "vitals_reading", "event_ts": "2026-09-21T14:03:11Z",
  "patient_id": "pt_00042", "encounter_id": "enc_10293", "schema_version": 1,
  "patient": {"mrn": "MRN-000123", "full_name": "...", "birth_date": "1968-04-02", "gender": "female", "region": "midwest"},
  "encounter": {"encounter_type": "inpatient", "unit": "ICU", "attending_provider_id": "prov_042", "status": "in-progress"},
  "transfer": {"from_unit": "ED", "to_unit": "ICU"},
  "vital": {"itemid": "heart_rate", "value": 88.0, "valueuom": "bpm"},
  "notes": "free-text nurse/charting note"
}
```

`patient`/`encounter` populate only on `admitted`; `transfer` only on
`transferred`; `vital` only on `vitals_reading`. `notes` can appear on any
event and is the deliberate prompt-injection attack surface — see
`simulator/chaos.build_prompt_injection_payload` and
`agent/tools/governance_guard.scan_for_injection`.

`dim_patients.full_name`/`dim_patients.mrn` are masked columns (see
`common.contracts.MASKED_COLUMNS`), enforced both at the Unity Catalog layer
and in-process before any row reaches the orchestrator agent's LLM calls.

## Dataset sourcing

The user-facing request behind this project was "download a dataset" — here
that resolves to a small, real, **openly-licensed synthetic** sample under
[`data/reference/synthea_sample/`](data/reference/synthea_sample/) (Synthea,
Apache-2.0, MITRE, zero real PHI). **That file is a documented reference
artifact only — the pipeline and simulator do not read it at runtime.**

The actual real-time stream is produced by this repo's own Python generator
(`simulator/`), parameterized for scale (default 1,000, configurable to
100,000+ concurrently "admitted" simulated patients) rather than a replay of
the sample file. Its entity shapes are modeled on **Synthea's FHIR resources**
(Patient, Encounter — https://synthea.mitre.org) and its vitals-over-time
structure is modeled on **MIMIC-IV's `chartevents` table** (`subject_id,
charttime, itemid, value, valueuom` — Johnson et al., MIMIC-IV, PhysioNet),
cited for schema-shape credibility only — no MIMIC-IV data or credentialing
is involved. Vital-sign ranges (`common.contracts.VITAL_RANGES`) are original
illustrative approximations, not derived from either dataset's real values.
See `simulator/domain.py`'s module docstring for the swap-in path to real
Synthea/MIMIC-IV exports.

## Why DLT end-to-end / why Kafka + Autoloader-as-secondary

See [`docs/architecture.md`](docs/architecture.md) for the full rationale —
short version: one consistent DLT expectations model end-to-end instead of
mixing DLT and hand-rolled Structured Streaming; Kafka for the genuinely
continuous ADT+vitals stream, Autoloader on a triggered (not continuous)
pipeline for the slow-changing provider roster, which is a real cost
decision, not incidental.

## Setup order

```bash
gh auth login                                  # only needed for the push step, not local dev
uv sync --extra dev                            # install runtime + dev deps
docker compose up -d                           # local Redpanda broker + console (localhost:8080)
python -m simulator.producer --patients 1000 --duration 60   # produce a patient-events stream
python -m simulator.autoloader_feed                          # drop provider-roster batch files
pytest                                          # full suite, mocked Anthropic client, no network calls
# Databricks-only, requires a real workspace + DATABRICKS_HOST/DATABRICKS_TOKEN:
databricks bundle validate
databricks bundle deploy -t dev
```

## Status

**Locally runnable and tested:**
- `simulator/` — patient-event and provider-roster generators, and the
  `Population.tick()` loop, driven by an injectable clock (no real sleep in
  the core loop) for deterministic tests.
- `agent/` — the full orchestrator (`OrchestratorAgent`), both DE and DA
  tool sets, and the masking/injection-defense guardrail wiring. The
  Anthropic client is fully mockable (`Claude` is injected, never
  constructed globally) — the pytest suite makes **zero real network/LLM
  calls** and passes with no `ANTHROPIC_API_KEY` set.
- `data/seed/gold_seed.sql` + DuckDB — a real, queryable local stand-in for
  the Gold schema that `agent/tools/data_query.py` runs against.
- `pytest` (9 test files) and `ruff check .` both pass locally.

**Databricks-only / structurally complete but not executable here (no live
workspace exists in this environment):**
- Everything under `pipeline/` (DLT bronze/silver/gold) and
  `governance/05_unity_catalog/` (Unity Catalog SQL) — correct PySpark/DLT
  and SQL, but only resolves once the `databricks` extra and a real cluster
  exist.
- `resources/*.yml` and `databricks.yml` — a structurally valid Databricks
  Asset Bundle with placeholder workspace hosts; `databricks bundle validate`
  is gated in CI on `DATABRICKS_HOST`/`DATABRICKS_TOKEN` secrets and skips
  gracefully without them.
- `resources/workflows.yml:agent_pipeline_healthcheck` — runs the DE-mode
  orchestrator on a schedule against a real cluster; described for
  completeness, not run here.
- Docker Compose (Redpanda) has not been run end-to-end inside this
  particular sandbox, but is the same, standard local-Kafka pattern used
  elsewhere and is expected to work with `docker compose up -d`.

Swap in a real Databricks workspace, real Synthea/MIMIC-IV exports (see the
dataset-sourcing section above), and real credentials before treating any of
the Databricks-only pieces as more than structurally correct.
