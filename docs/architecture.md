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
              │                                │
              ▼                                ▼
        run_tool_loop(...) ─────────────► _dispatch(tool_name, tool_input)
```

## DE mode: keep the pipeline healthy

`agent/tools/pipeline_health.py` exposes read tools (`check_expectation_metrics`,
`check_job_status`, `detect_schema_drift`) and remediation tools
(`quarantine_bad_records`, `restart_pipeline`, `notify_and_page`), all backed
by a local JSON state file so they're fully testable without a live
Databricks workspace. Each function's docstring names the real Databricks
REST API it stands in for.

The pipeline itself encodes a hard rule the agent must respect:
`known_event_type` in `pipeline/03_silver/silver_encounters.py` is a hard-stop
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
Silver: `dlt.apply_changes` (CDC/upsert) for mutable state (`dim_patients`,
`fct_encounters`), vs. `withWatermark` + `dropDuplicatesWithinWatermark`
(append-only) for the immutable vitals time series
(`pipeline/03_silver/silver_vitals.py` — see its docstring for the full
rationale).

## Guardrails

Production AI-agent guardrail patterns, each enforced in code (not just
requested via a system prompt) unless noted otherwise. "Implementing
file" is where the guardrail runs; "Test" is what proves it.

| Guardrail | What it does | Implementing file | Test |
|---|---|---|---|
| Input validation | `common.contracts.PatientEvent`/`VitalReading` (Pydantic) reject malformed envelopes (bad `event_type`/`itemid`, wrong types, unknown fields) at construction; out-of-range vitals are flagged, not rejected — mirrors DLT `expect` vs `expect_or_drop` | `common/contracts.py` | `tests/test_contracts_pydantic.py` |
| Input-size limit | `MAX_NOTES_LENGTH` caps text scanned by the injection guard, defending against a padded-out adversarial payload | `agent/tools/governance_guard.py:scan_for_injection` | `tests/test_prompt_injection_guard.py` |
| Output-size / groundedness | A zero-row `query_gold_table` result is short-circuited to a fixed refusal instead of flowing to the model as ordinary data; oversized results are capped (`MAX_TOOL_RESULT_ROWS`, `MAX_TOOL_RESULT_CHARS`) before ever reaching `messages` | `agent/orchestrator.py:_dispatch_query_gold_table`, `_apply_output_size_guard` | `tests/test_groundedness_refusal.py` |
| Tool allowlisting / blast radius | Only 6 DE tools and 1 DA tool are ever passed to Claude; no destructive (drop/delete/truncate-shaped) tool exists anywhere in the codebase | `agent/orchestrator.py:DE_TOOLS`/`DA_TOOLS` | `tests/test_tool_allowlist.py` |
| Escalation ceiling | A (table, expectation) pair that's already been "fixed" 3 times and is still failing is refused a 4th quarantine attempt and forced to `notify_and_page` — enforced in the dispatch wrapper, since the model can't be trusted to self-limit a retry loop | `agent/orchestrator.py:_dispatch_quarantine`, `ESCALATION_CEILING` | `tests/test_escalation_ceiling.py` |
| Kill switch | `autonomous_remediation_enabled` (state-file flag, default true) gates `quarantine_bad_records`/`restart_pipeline`; read-only health checks and `notify_and_page` are never gated | `agent/orchestrator.py:_autonomous_remediation_enabled`, `_killswitch_refusal` | `tests/test_kill_switch.py` |
| Immutable audit trail | Every `_dispatch()` call appends one line to an append-only JSONL log (masked output only, never raw pre-mask values); opened with `"a"`, never rewritten | `agent/orchestrator.py:_append_audit_log` | `tests/test_audit_trail.py` |
| Behavioral anomaly detection | Pure function counts `notify_and_page` calls in a trailing time window against a threshold — signals either a real widespread incident or the agent itself misbehaving | `agent/orchestrator.py:detect_anomalous_activity` | `tests/test_anomaly_detection.py` |
| Retry + timeout | The Anthropic call and the Kafka `produce()` call are each wrapped in exponential-backoff retry (tenacity), retrying only transient/network-shaped errors; the Anthropic call also carries an explicit timeout | `agent/llm.py:Claude._create_message`, `simulator/producer.py:_produce_with_retry` | `tests/test_llm_retry.py` |
| Rate limiting | `--max-events-per-second` throttles the simulator's aggregate publish rate — a per-source rate limit distinct from the existing per-vital cadence jitter | `simulator/producer.py:RateLimiter` | (exercised via `RateLimiter.acquire`; no live-broker integration test — see Status in README.md) |
| Graceful degradation | If the Claude call path raises, `handle()` catches it and returns a safe, clearly-worded result instead of crashing — the pipeline's own DLT hard-stop expectations keep enforcing correctness independently of whether the agent is reachable | `agent/orchestrator.py:handle` | `tests/test_llm_retry.py` (retry path); degradation path covered by `handle`'s own `except` block |
| Minimum-necessary-access | `orchestrator_agent` is granted exactly `USE CATALOG` + `USE SCHEMA, SELECT` on `gold` — no write access, no Bronze/Silver, never `phi_unmasked` | `governance/05_unity_catalog/catalog_and_grants.sql`, `governance/05_unity_catalog/access_policy_notes.md` | `tests/test_contract_consistency.py` (masking cross-check); grant itself is Databricks-only SQL, not independently testable here |
| Observability tracing | Optional Langfuse spans per turn/tool call (turn number, tool name, latency, token usage) — a genuine no-op with zero network calls unless `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` are set | `agent/llm.py:Tracer` | `tests/test_llm_retry.py` exercises the no-op default; real Langfuse spans are not exercised against a live account in this environment |

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
