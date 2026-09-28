# Databricks Agentic DE

Project 2 of a two-part portfolio series (Snowflake + Databricks — Project 1
is [`snowflake-ai-data-agent`](../snowflake-ai-data-agent), backed by
Snowflake's no-code Cortex Analyst). This project takes the harder, more
differentiated path: **healthcare, real-time
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

## Guardrails

Beyond PHI masking and prompt-injection defense (above), the orchestrator
carries a fuller set of production AI-agent guardrail patterns — each
enforced in code, not just requested in a system prompt. Full table with
implementing-file/test pointers: [`docs/architecture.md`](docs/architecture.md#guardrails).

| Guardrail | Summary |
|---|---|
| Input validation | Pydantic models (`common/contracts.py`) reject malformed event envelopes at construction |
| Groundedness | A zero-row Gold-table query returns a fixed refusal, never silent-flows to the model as data |
| Tool allowlisting / blast radius | No destructive tool exists anywhere; DE/DA tool lists are structurally verified |
| Escalation ceiling | A failure "fixed" 3 times and still failing forces `notify_and_page`, in code |
| Kill switch | A state-file flag can disable auto-remediation while read-only checks keep working |
| Immutable audit trail | Every dispatch appends one line to an append-only JSONL log (masked output only) |
| Anomaly detection | A pure function flags a suspicious spike in escalations within a time window |
| Retry / timeout | Anthropic + Kafka calls retry transient failures only, with exponential backoff |
| Rate limiting | `--max-events-per-second` throttles the simulator's aggregate publish rate |
| Graceful degradation | A Claude API outage returns a safe result instead of crashing the caller |
| Minimum-necessary-access | The agent's Unity Catalog role is SELECT-only on Gold, never PHI-unmasked |
| Observability tracing | Optional Langfuse (LLM calls) + OpenTelemetry (infra) spans — no-op unless configured |
| Secret management | GCP Secret Manager when `GCP_PROJECT_ID` is set, else a plain env var — no-op unless configured |

## Layout

```
databricks-agentic-de/
├── pipeline/                # Databricks-only: DLT bronze/silver/gold, structurally complete, not run here
│   ├── common/schemas.py
│   ├── 01_ingest/  02_bronze/  03_silver/  04_gold/
├── governance/05_unity_catalog/   # UC grants, column masks, row filters, BigQuery federation example (Databricks-only)
├── common/contracts.py       # single source of truth: event schema, allowed types, masked columns, Pydantic models
├── agent/                    # the real, locally-runnable deliverable
│   ├── llm.py                # thin Claude tool-loop wrapper, no agent framework; retry/timeout/optional Langfuse tracing
│   ├── orchestrator.py       # OrchestratorAgent: DE/DA routing + guardrail wiring, MCP-routed DE-mode dispatch
│   ├── mcp_bridge.py         # MCP client: talks to mcp_server/server.py over stdio
│   ├── otel.py                # OpenTelemetry tracing setup, no-op unless OTEL_EXPORTER_OTLP_ENDPOINT is set
│   ├── secrets.py             # GCP Secret Manager, no-op unless GCP_PROJECT_ID is set
│   ├── prompts.py  state.py
│   └── tools/{pipeline_health.py, data_query.py, governance_guard.py, anomaly_score.py, knowledge_search.py}
├── mcp_server/server.py      # real MCP server exposing the tools above over stdio
├── ml/                       # train_anomaly_model.py (MLflow + IsolationForest) + models/vitals_anomaly.onnx
├── simulator/                # streaming patient-event + provider-roster generators
│   ├── domain.py  population.py  producer.py (OTel-instrumented)  autoloader_feed.py  chaos.py
├── infra/
│   ├── terraform/            # GCP resources Asset Bundles don't cover — not applied here, see its README.md
│   └── cloudrun/              # Dockerfile + deploy.sh for running the simulator on Cloud Run — not run here
├── data/
│   ├── reference/synthea_sample/   # reference artifact only — not read at runtime
│   ├── knowledge/clinical_protocols.md   # demo content for agent/tools/knowledge_search.py
│   ├── seed/gold_seed.sql
│   └── state/pipeline_state.example.json
├── docs/comparisons/          # Genie/Agent Bricks/Vertex AI evaluation plans — pending a live workspace
├── tests/                    # mocked Anthropic client, zero network calls
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
that resolves to a small, real, **openly-licensed synthetic** sample:
[`synthea_sample_data_csv_latest.zip`](data/reference/synthea_sample/synthea_sample_data_csv_latest.zip)
(100 synthetic patients, 18 CSVs, ~5.7 MB, downloaded from MITRE's official
[Synthea downloads page](https://synthea.mitre.org/downloads) — zero real
PHI, license and citation in
[`data/reference/synthea_sample/SOURCE.md`](data/reference/synthea_sample/SOURCE.md)).
**That file is a documented reference artifact only — the pipeline and
simulator do not read it at runtime** (see `SOURCE.md` for the swap-in path
if you want to build against it directly).

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
  tool sets, and the masking/injection-defense guardrail wiring, plus the
  fuller guardrail set in [`docs/architecture.md`](docs/architecture.md#guardrails)
  (escalation ceiling, kill switch, audit trail, anomaly detection, retry/
  timeout, graceful degradation). The Anthropic client is fully mockable
  (`Claude` is injected, never constructed globally) — the pytest suite makes
  **zero real network/LLM calls** and passes with no `ANTHROPIC_API_KEY` set.
- `data/seed/gold_seed.sql` + DuckDB — a real, queryable local stand-in for
  the Gold schema that `agent/tools/data_query.py` runs against.
- `pytest` and `ruff check .` both pass locally.
- Tech stack additions backing the guardrails above: **Pydantic** (event
  envelope validation, `common/contracts.py`), **Tenacity** (retry/backoff,
  `agent/llm.py` + `simulator/producer.py`), **structlog** (structured
  warning/error logs on guardrail trips), and optional **Langfuse** tracing
  (`agent/llm.py:Tracer` — a genuine no-op with zero network calls unless
  `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` are set; not exercised against a
  real Langfuse account in this environment).
- ML lifecycle: **MLflow** (local file-based tracking + Model Registry,
  `ml/train_anomaly_model.py`), **scikit-learn** (`IsolationForest`),
  **skl2onnx** + **onnxruntime** (export/serve the trained model as ONNX,
  `agent/tools/anomaly_score.py`) — see
  [`docs/architecture.md`](docs/architecture.md#ml-lifecycle-mlflow--onnx-anomaly-detection).
- Agent protocol: the official **`mcp`** SDK — DE-mode tools are served over
  a real MCP server (`mcp_server/server.py`) and called through an MCP client
  bridge (`agent/mcp_bridge.py`), not just in-process function calls — see
  [`docs/architecture.md`](docs/architecture.md#mcp-tool-architecture).
- Infrastructure: **OpenTelemetry** (`opentelemetry-api`/`-sdk`; spans for
  the simulator's publish loop and DE-mode tool calls, no-op unless
  `OTEL_EXPORTER_OTLP_ENDPOINT` is set) and **`google-cloud-secret-manager`**
  (`agent/secrets.py`, same no-op-unless-`GCP_PROJECT_ID`-is-set pattern as
  Langfuse).

## Platform-native alternatives considered

This project's differentiator is a hand-built orchestrator agent instead of a
vendor no-code tool (see the intro above) — but that's a trade-off worth
actually evaluating against the platform-native alternatives, not just
asserting. Three evaluation plans (criteria + methodology, explicitly **not**
completed comparisons — no scores or "X beat Y" conclusions exist yet) live
in [`docs/comparisons/`](docs/comparisons/):

- [`genie_vs_custom_agent.md`](docs/comparisons/genie_vs_custom_agent.md) —
  Databricks Genie vs. this project's DA-mode Q&A path.
- [`agent_bricks_vs_custom_agent.md`](docs/comparisons/agent_bricks_vs_custom_agent.md) —
  Databricks Agent Bricks vs. this project's hand-built orchestrator +
  MCP tool layer.
- [`vertex_ai_vs_mlflow.md`](docs/comparisons/vertex_ai_vs_mlflow.md) —
  Vertex AI Model Registry/Endpoints vs. this project's local MLflow + ONNX
  ML lifecycle.

Each is pending a real, GCP-connected Databricks workspace that was still
being set up as of this task and was not yet live in this environment — see
each document's own `## Status: Pending` section.

## Status

Three categories, precisely: what actually runs and is tested locally right
now; what is Databricks/GCP-platform code that structurally only ever
resolves on that platform (independent of any particular workspace); and
what is new scaffolding that specifically awaits this project's real,
GCP-connected Databricks workspace (in progress, not yet live here) before it
can be exercised for real.

**Locally runnable and tested:**
- `simulator/` — patient-event and provider-roster generators, and the
  `Population.tick()` loop, driven by an injectable clock (no real sleep in
  the core loop) for deterministic tests. `simulator/producer.py:send` is
  also OpenTelemetry-instrumented (one span per event published).
- `agent/` — the full orchestrator (`OrchestratorAgent`), both DE and DA
  tool sets, and the masking/injection-defense guardrail wiring, plus the
  fuller guardrail set in [`docs/architecture.md`](docs/architecture.md#guardrails)
  (escalation ceiling, kill switch, audit trail, anomaly detection, retry/
  timeout, graceful degradation, OpenTelemetry tracing, GCP Secret Manager
  integration). The Anthropic client is fully mockable (`Claude` is injected,
  never constructed globally) — the pytest suite makes **zero real
  network/LLM calls** and passes with no `ANTHROPIC_API_KEY` set.
- `agent/mcp_bridge.py` + `mcp_server/server.py` — a real MCP server exposing
  the pipeline-health, data-query, and anomaly-scoring tools, and a real MCP
  client bridge DE-mode dispatch is routed through; `tests/test_mcp_server_tools.py`
  spins up the real subprocess over stdio (a local pipe, not a network call)
  and re-proves the masking and audit-trail guarantees hold with MCP-routed
  dispatch.
- `ml/train_anomaly_model.py` + `agent/tools/anomaly_score.py` — a real,
  trained `IsolationForest` anomaly detector, tracked via a local file-based
  MLflow run and registry, exported to the committed
  `ml/models/vitals_anomaly.onnx` and served locally via `onnxruntime`. Tests
  train a tiny model into a `tmp_path` rather than depending on the full
  training run's speed.
- `data/seed/gold_seed.sql` + DuckDB — a real, queryable local stand-in for
  the Gold schema that `agent/tools/data_query.py` runs against.
- `pytest` and `ruff check .` both pass locally.

**Databricks-only / structurally complete but not executable here (no live
workspace exists in this environment) — a platform requirement, independent
of workspace setup progress:**
- Everything under `pipeline/` (DLT bronze/silver/gold) and
  `governance/05_unity_catalog/` (Unity Catalog SQL, plus the new
  `bigquery_federation_example.sql` Lakehouse Federation scaffold) — correct
  PySpark/DLT and SQL, but only resolves once the `databricks` extra and a
  real cluster exist.
- `resources/*.yml` and `databricks.yml` — a structurally valid Databricks
  Asset Bundle targeting **GCP** (workspace host is a `*.gcp.databricks.com`
  placeholder, `provider_landing_path` defaults to a `gs://` bucket path);
  `databricks bundle validate` is gated in CI on
  `DATABRICKS_HOST`/`DATABRICKS_TOKEN` secrets and skips gracefully without
  them. GCP Pub/Sub is noted inline (`databricks.yml`) as the cloud-native
  managed alternative to the self-hosted Kafka/Redpanda path used for local
  dev in this repo (and provisioned structurally in `infra/terraform/main.tf`
  below).
- `resources/workflows.yml:agent_pipeline_healthcheck` — runs the DE-mode
  orchestrator on a schedule against a real cluster; described for
  completeness, not run here.
- Docker Compose (Redpanda) has not been run end-to-end inside this
  particular sandbox, but is the same, standard local-Kafka pattern used
  elsewhere and is expected to work with `docker compose up -d`.

**Pending a live, GCP-connected Databricks workspace (in progress, not yet
live in this environment) — new in this pass, scaffolded and unit-tested
where the scaffold has a graceful-degradation path, but genuinely not
exercised against real infrastructure:**
- `agent/tools/knowledge_search.py` + `data/knowledge/clinical_protocols.md`
  — Databricks Vector Search integration; degrades to a clear "not
  configured" result with zero network calls when unconfigured
  (`tests/test_knowledge_search_tool.py` proves the degradation path, not a
  real vector search).
- `infra/terraform/` — GCS bucket, service account/IAM, Pub/Sub topic, and
  Secret Manager containers for the GCP resources Asset Bundles don't cover;
  never `terraform init`/`plan`/`apply`'d here (see its own README.md).
- `governance/05_unity_catalog/bigquery_federation_example.sql` —
  illustrative Lakehouse Federation SQL, not run against a real BigQuery
  dataset.
- `infra/cloudrun/` — a Dockerfile + `gcloud run deploy` script for running
  the simulator on Cloud Run; never built or deployed here.
- The three [`docs/comparisons/`](docs/comparisons/) evaluation plans —
  criteria and methodology only, explicitly marked `## Status: Pending`, no
  fabricated results.

Swap in a real Databricks workspace, real Synthea/MIMIC-IV exports (see the
dataset-sourcing section above), and real credentials before treating any of
the Databricks-only or pending-workspace pieces as more than structurally
correct.
