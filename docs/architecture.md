# Architecture

## Two modes, one entry point

`agent.orchestrator.OrchestratorAgent.handle(request, mode="auto")` is the
single entry point. In `"auto"` mode it makes one small Claude call
(`agent/prompts.py:MODE_ROUTER_PROMPT`) that returns `{"mode": "de"|"da"}`,
parsed defensively (find the outermost `{...}`, `json.loads`, fall back to
`"da"` on any exception) — mirroring the same routing pattern the sibling
`abhay` project's `ManagerAgent._route` uses.

```
                     handle(request, mode="auto")
                              │
                     ┌────────┴────────┐
                     │   _route(...)   │  one small Claude call
                     └────────┬────────┘
                 mode="de"    │    mode="da"
              ┌───────────────┴───────────────┐
              ▼                                ▼
     SYSTEM_PROMPT_DE                  SYSTEM_PROMPT_DA
     + pipeline_health.* tools         + query_gold_table tool
     + score_vitals_anomaly            (direct in-process dispatch —
              │                         see "MCP tool architecture" below)
              ▼                                │
        run_tool_loop(...) ─────────────► _dispatch(tool_name, tool_input)
              │
              ▼ (DE-mode tools only)
      MCPToolBridge.dispatch(...) ──stdio──► mcp_server/server.py (subprocess)
```

## DE mode: keep the pipeline healthy

`agent/tools/pipeline_health.py` exposes read tools (`check_expectation_metrics`,
`check_job_status`, `detect_schema_drift`) and remediation tools
(`quarantine_bad_records`, `restart_pipeline`, `notify_and_page`), all backed
by a local JSON state file so they're fully testable without a live
Databricks workspace. Each function's docstring names the real Databricks
REST API it stands in for. `agent/tools/anomaly_score.py:score_vitals_anomaly`
is a seventh DE-mode tool — see "ML lifecycle" below for what it does and how
it's trained. All seven are dispatched through the MCP bridge described in
"MCP tool architecture" below, not called in-process directly.

The pipeline itself encodes a hard rule the agent must respect:
`known_event_type` in `pipeline/03_silver/silver_patient_events.py` is a hard-stop
expectation (`dlt.expect_or_fail`) — an event_type outside the four the
system understands is a genuine contract break, not a data-quality nuisance.
The DE system prompt instructs the agent to escalate (`notify_and_page`)
rather than attempt to quarantine or restart past a hard-stop failure. See
`tests/test_chaos_remediation.py` for the two contrasting scripted scenarios
(auto-fixable vs. must-escalate).

## DA mode: answer questions over governed data, safely

`agent/tools/data_query.py:query_gold_table` runs SQL against a local DuckDB
file seeded from `data/seed/gold_seed.sql` (swap point for
`databricks-sql-connector` in a real deployment is marked in that file).

The guardrail wiring lives in `OrchestratorAgent._dispatch`:

1. `query_gold_table` results are passed through
   `governance_guard.enforce_masking(table, rows, role="orchestrator_agent")`
   **before** they are ever placed into a `ToolResult.content`. Since
   `messages` (the transcript sent to Claude) only ever receives
   `ToolResult.content`, the raw, unmasked `full_name`/`mrn` values
   architecturally never reach the model — there is no prompt that can make
   Claude repeat a value it was never given. `tests/test_masking_guard.py`
   proves this end-to-end by scripting a fake Claude that requests
   `dim_patients` and asserting the raw seed values never appear anywhere in
   `messages`.
2. Every tool result's content (masked or not) is scanned with
   `governance_guard.scan_for_injection` — a heuristic, best-effort layer
   documented as such in its own docstring. Flagged content (most notably an
   injection attempt planted in a patient event's free-text `notes` field —
   see `simulator/chaos.build_prompt_injection_payload`) is wrapped in
   `<untrusted_data>...</untrusted_data>` before it's appended to `messages`.
   `SYSTEM_PROMPT_DA` explicitly instructs the model to treat that tag's
   contents, and any tool-returned text in general, as data — never as
   instructions. `tests/test_prompt_injection_guard.py` proves the wrapping
   happens.

The database-level backstop for (1) is
`governance/05_unity_catalog/row_filters_and_masking.sql`'s column masks,
gated on `is_account_group_member('phi_unmasked')` — the `orchestrator_agent`
Unity Catalog group is deliberately never granted that membership
(`governance/05_unity_catalog/catalog_and_grants.sql`). Masking is therefore
enforced twice, independently, at two different layers.

## Why DLT end-to-end

Bronze → Silver → Gold all run as Delta Live Tables, not a mix of DLT and
hand-rolled Structured Streaming jobs. That gives one consistent
expectations/observability model (`dlt.expect*`) the DE tools can query
uniformly, and one deployment unit (`resources/dlt_pipeline.yml`) instead of
juggling job dependencies by hand.

Two write patterns are used deliberately for two different semantics in
Silver: `dlt.apply_changes` (CDC/upsert) for mutable state (`silver_dim_patients`,
`silver_fct_encounters`), vs. `withWatermark` + `dropDuplicatesWithinWatermark`
(append-only) for the immutable vitals time series
(`pipeline/03_silver/silver_vitals.py` — see its docstring for the full
rationale).

## ML lifecycle: MLflow + ONNX anomaly detection

`common.contracts.VITAL_RANGES` (used by `simulator/domain.py` and the
Silver-layer DLT expectations) is a per-column range check — it can only ever
flag one out-of-band vital value at a time. `ml/train_anomaly_model.py`
trains a real `sklearn.ensemble.IsolationForest` — unsupervised, joint
multi-vital anomaly detection — on synthetic vitals panels generated by
calling `simulator/domain.py`'s own `generate_vitals_event` once per vital
type per row (the same generator that drives the Kafka stream, not a
separately-invented data source). It can catch a reading where every
individual vital is technically in-range but the *combination* isn't (e.g. a
normal heart rate with a critically low SpO2 and an elevated respiratory rate
at once).

Training runs against a local, file-based MLflow tracking URI (`./mlruns`,
gitignored) — params (contamination rate, `n_estimators`), the held-out
fraction-flagged-anomalous metric, and the sklearn model itself are logged
and registered in the local MLflow Model Registry (`vitals_anomaly_detector`)
via `mlflow.register_model`, all with zero external MLflow server. The
trained model is then exported to ONNX (`skl2onnx.convert_sklearn`) and
committed at `ml/models/vitals_anomaly.onnx` — a small, portable,
git-trackable artifact.

At inference time, `agent/tools/anomaly_score.py:score_vitals_anomaly` loads
that ONNX file via `onnxruntime.InferenceSession` (cached across calls,
`functools.lru_cache`) and scores a single reading — no MLflow, no
scikit-learn, and no network call needed to serve a prediction, which is the
whole point of the ONNX export: training-time dependencies (mlflow,
scikit-learn, skl2onnx) never need to be on the hot inference path.
`tests/test_anomaly_score_tool.py` trains a genuinely tiny model (a few
hundred rows) into a `tmp_path` for testing, rather than depending on the
full multi-thousand-row training run or the committed artifact being fast
enough for CI.

## MCP tool architecture

DE-mode tool calls (`agent/orchestrator.py`'s `_dispatch_pipeline_health`,
`_dispatch_quarantine`, `_dispatch_restart`, and the `score_vitals_anomaly`
branch) are no longer in-process Python calls into
`agent/tools/pipeline_health.py`/`agent/tools/anomaly_score.py` directly.
Instead, `agent/mcp_bridge.py:MCPToolBridge` speaks the real Model Context
Protocol, over stdio, to `mcp_server/server.py` — a real MCP server (built on
the official `mcp` SDK's `MCPServer`) that exposes those same functions (plus
`agent/tools/data_query.py:query_gold_table`, for protocol-surface
completeness) as MCP tools with schemas derived from their actual Python
signatures.

**Why MCP instead of just calling the functions directly, when it's still the
same process's own tools**: protocol standardization and tool reusability.
Once a tool is exposed over MCP, ANY MCP-speaking client — not just this
project's own `OrchestratorAgent` — can discover and call it with zero
project-specific glue code: the same `mcp_server/server.py` could be pointed
at from a completely different agent framework, a different LLM provider's
tool-calling loop, or an interactive MCP inspector, without touching this
project's Python at all. It also forces a clean interface boundary (JSON-
schema-typed inputs/outputs over a stable protocol) between "the tools" and
"the thing calling them," the same value proposition a REST API gives two
services that happen to currently run on the same machine.

**Performance note**: a fresh MCP server subprocess re-imports
anthropic/structlog/onnxruntime/the mcp SDK on every start (~3-4s, dominated
by those packages' own import time). Spawning one per tool call would make a
test suite exercising dozens of DE-mode dispatches unusably slow, so
`MCPToolBridge` starts the subprocess and MCP session lazily on first use and
keeps them alive on a dedicated background thread for the bridge's lifetime;
`agent.mcp_bridge.get_default_bridge()` is a process-wide singleton so the
one-time startup cost is paid once per process (e.g. once for an entire
pytest run), not once per `OrchestratorAgent()`.

**What did NOT change**: DA-mode's `query_gold_table` call
(`_dispatch_query_gold_table`) deliberately stays a direct in-process call,
not MCP-routed — PHI masking (`governance_guard.enforce_masking`) must be the
very next thing that happens to a raw row, and routing that specific call
through an extra process boundary first would add risk/complexity to the
single most safety-critical path in this codebase for no real benefit (see
that method's own comment in `agent/orchestrator.py`). Every guardrail
described in this document runs exactly the same way regardless of which
path a given tool call takes — the MCP switch changed a transport, not the
guardrail wiring. `tests/test_mcp_server_tools.py` explicitly re-proves the
audit-trail and masking guarantees hold with MCP-routed DE-mode dispatch.

## Infrastructure as code: Terraform + Asset Bundles

Two separate infra-as-code tools manage two separate layers, deliberately not
overlapping:

- **Databricks Asset Bundles** (`databricks.yml` + `resources/*.yml`) manage
  **Databricks-side** objects: the DLT pipeline definition, the job/workflow
  schedule, workspace-level target configuration.
- **Terraform** (`infra/terraform/`) manages the **underlying GCP
  infrastructure** those Databricks-side jobs read from, write to, and
  authenticate against: the GCS landing bucket, a service account scoped via
  IAM to exactly that bucket, a Pub/Sub topic (the GCP-native alternative to
  self-hosted Kafka already noted inline in `databricks.yml`), and Secret
  Manager secret containers for `ANTHROPIC_API_KEY`/the Langfuse keys.

Neither tool manages the other's layer; a real deployment applies the
Terraform first (so the bucket/service-account/topic exist), then runs
`databricks bundle deploy` against them. **Like `pipeline/` and
`governance/05_unity_catalog/`, the Terraform configuration has not been
applied anywhere in this environment** — see `infra/terraform/README.md` for
exactly what "structurally correct, not applied" means here and the commands
you'd run against a real GCP project.

## Guardrails

Production AI-agent guardrail patterns, each enforced in code (not just
requested via a system prompt) unless noted otherwise. "Implementing
file" is where the guardrail runs; "Test" is what proves it.

| Guardrail | What it does | Implementing file | Test |
|---|---|---|---|
| Input validation | `common.contracts.PatientEvent`/`VitalReading` (Pydantic) reject malformed envelopes (bad `event_type`/`itemid`, wrong types, unknown fields) at construction; out-of-range vitals are flagged, not rejected — mirrors DLT `expect` vs `expect_or_drop` | `common/contracts.py` | `tests/test_contracts_pydantic.py` |
| Input-size limit | `MAX_NOTES_LENGTH` caps text scanned by the injection guard, defending against a padded-out adversarial payload | `agent/tools/governance_guard.py:scan_for_injection` | `tests/test_prompt_injection_guard.py` |
| Output-size / groundedness | A zero-row `query_gold_table` result is short-circuited to a fixed refusal instead of flowing to the model as ordinary data; oversized results are capped (`MAX_TOOL_RESULT_ROWS`, `MAX_TOOL_RESULT_CHARS`) before ever reaching `messages` | `agent/orchestrator.py:_dispatch_query_gold_table`, `_apply_output_size_guard` | `tests/test_groundedness_refusal.py` |
| Tool allowlisting / blast radius | Only 7 DE tools and 1 DA tool are ever passed to Claude; no destructive (drop/delete/truncate-shaped) tool exists anywhere in the codebase | `agent/orchestrator.py:DE_TOOLS`/`DA_TOOLS` | `tests/test_tool_allowlist.py` |
| Escalation ceiling | A (table, expectation) pair that's already been "fixed" 3 times and is still failing is refused a 4th quarantine attempt and forced to `notify_and_page` — enforced in the dispatch wrapper, since the model can't be trusted to self-limit a retry loop | `agent/orchestrator.py:_dispatch_quarantine`, `ESCALATION_CEILING` | `tests/test_escalation_ceiling.py` |
| Kill switch | `autonomous_remediation_enabled` (state-file flag, default true) gates `quarantine_bad_records`/`restart_pipeline`; read-only health checks and `notify_and_page` are never gated | `agent/orchestrator.py:_autonomous_remediation_enabled`, `_killswitch_refusal` | `tests/test_kill_switch.py` |
| Immutable audit trail | Every `_dispatch()` call appends one line to an append-only JSONL log (masked output only, never raw pre-mask values); opened with `"a"`, never rewritten | `agent/orchestrator.py:_append_audit_log` | `tests/test_audit_trail.py` |
| Behavioral anomaly detection | Pure function counts `notify_and_page` calls in a trailing time window against a threshold — signals either a real widespread incident or the agent itself misbehaving | `agent/orchestrator.py:detect_anomalous_activity` | `tests/test_anomaly_detection.py` |
| Retry + timeout | The Anthropic call and the Kafka `produce()` call are each wrapped in exponential-backoff retry (tenacity), retrying only transient/network-shaped errors; the Anthropic call also carries an explicit timeout | `agent/llm.py:Claude._create_message`, `simulator/producer.py:_produce_with_retry` | `tests/test_llm_retry.py` |
| Rate limiting | `--max-events-per-second` throttles the simulator's aggregate publish rate — a per-source rate limit distinct from the existing per-vital cadence jitter | `simulator/producer.py:RateLimiter` | (exercised via `RateLimiter.acquire`; no live-broker integration test — see Status in README.md) |
| Graceful degradation | If the Claude call path raises, `handle()` catches it and returns a safe, clearly-worded result instead of crashing — the pipeline's own DLT hard-stop expectations keep enforcing correctness independently of whether the agent is reachable | `agent/orchestrator.py:handle` | `tests/test_llm_retry.py` (retry path); degradation path covered by `handle`'s own `except` block |
| Minimum-necessary-access | `orchestrator_agent` is granted exactly `USE CATALOG` + `USE SCHEMA, SELECT` on `gold` — no write access, no Bronze/Silver, never `phi_unmasked` | `governance/05_unity_catalog/catalog_and_grants.sql`, `governance/05_unity_catalog/access_policy_notes.md` | `tests/test_contract_consistency.py` (masking cross-check); grant itself is Databricks-only SQL, not independently testable here |
| Observability tracing (LLM calls) | Optional Langfuse spans per turn/tool call (turn number, tool name, latency, token usage) — a genuine no-op with zero network calls unless `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` are set | `agent/llm.py:Tracer` | `tests/test_llm_retry.py` exercises the no-op default; real Langfuse spans are not exercised against a live account in this environment |
| Observability tracing (infra) | OpenTelemetry spans (one per event published / per pipeline-health tool call) for the non-LLM parts — spans are always created, but export nowhere unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set, same no-op-unless-configured pattern | `agent/otel.py`, `simulator/producer.py:send`, `agent/tools/pipeline_health.py` | `tests/test_otel_tracing.py` (in-memory exporter; real OTLP export not exercised against a live collector in this environment) |
| Secret management | `get_secret(name)` resolves from GCP Secret Manager when `GCP_PROJECT_ID` is set, falling back to a plain environment variable otherwise (or on any Secret Manager error) — same no-op-unless-configured pattern as Langfuse tracing | `agent/secrets.py` | `tests/test_secrets.py` (mocked client + fallback path; zero real GCP calls) |

PHI masking and prompt-injection defense (the two guardrails this project led
with) are documented above in their own sections, not repeated in this table.

## Why Kafka + Autoloader-as-secondary

The patient-events stream (ADT + vitals) is genuinely continuous,
high-frequency event traffic — Kafka (Redpanda locally) is the right tool,
feeding a `continuous: true` DLT pipeline. The provider roster, by contrast,
is small, slow-changing, file-shaped reference data (HR/credentialing
exports) — Autoloader on a `continuous: false`, hourly-triggered pipeline
(`resources/workflows.yml:reference_data_hourly`) avoids paying for an
always-on cluster for data that changes rarely, which is a real cost/ops
decision rather than "two files because there happen to be two sources."
