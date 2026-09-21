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

## Why Kafka + Autoloader-as-secondary

The patient-events stream (ADT + vitals) is genuinely continuous,
high-frequency event traffic — Kafka (Redpanda locally) is the right tool,
feeding a `continuous: true` DLT pipeline. The provider roster, by contrast,
is small, slow-changing, file-shaped reference data (HR/credentialing
exports) — Autoloader on a `continuous: false`, hourly-triggered pipeline
(`resources/workflows.yml:reference_data_hourly`) avoids paying for an
always-on cluster for data that changes rarely, which is a real cost/ops
decision rather than "two files because there happen to be two sources."
