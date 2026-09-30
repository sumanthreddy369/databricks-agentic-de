# AGENTS.md

Instructions for AI coding agents working in this repository. See
[`README.md`](README.md) and [`docs/architecture.md`](docs/architecture.md)
for the full human-facing documentation this file summarizes.

## Project overview

A healthcare streaming-data platform (Kafka/Autoloader → Delta Live Tables →
Unity Catalog) kept healthy and made queryable by one hand-built Claude
tool-calling agent (`agent/orchestrator.py`), instead of a vendor no-code
tool. The agent runs in two modes:

- **DE mode**: keeps the streaming pipeline healthy (`agent/tools/pipeline_health.py`), self-remediating auto-fixable failures and escalating hard-stop contract breaks via `notify_and_page`.
- **DA mode**: answers clinical/ops questions over governed Gold tables (`agent/tools/data_query.py`), with PHI masking and prompt-injection defense enforced in code, not just in a system prompt.

The locally-runnable, tested deliverable is `agent/`, `mcp_server/`,
`simulator/`, and `ml/`. Everything under `pipeline/`, `governance/`,
`infra/`, `resources/`, and `databricks.yml` is Databricks/GCP-platform code
that is structurally correct but has never executed against a real
workspace — see `README.md`'s Feature status table before treating any of it
as more than that.

## Structure map

```
agent/                  # orchestrator agent: mode routing, dispatch, guardrails, MCP client
  orchestrator.py        # OrchestratorAgent — the central file; read this first
  llm.py                 # Claude wrapper: tool-calling loop, retry, optional Langfuse tracing
  mcp_bridge.py           # MCP client bridge to mcp_server/server.py
  databricks_client.py   # live-workspace REST client (SQL statements, Pipelines API); no-op unless DATABRICKS_* set
  tools/                 # pipeline_health.py (+ pipeline_health_live.py), data_query.py, governance_guard.py, anomaly_score.py, knowledge_search.py
mcp_server/server.py    # MCP server exposing agent/tools/* over stdio
common/contracts.py     # single source of truth for the event schema — change the schema HERE first
simulator/              # locally-runnable Kafka event + provider-roster generators
ml/                      # train_anomaly_model.py (IsolationForest -> MLflow -> ONNX) + committed models/vitals_anomaly.onnx
pipeline/                # Databricks-only DLT bronze/silver/gold — never run in this environment
governance/05_unity_catalog/  # Unity Catalog SQL — never run in this environment
infra/                  # Terraform + Cloud Run — never applied/deployed in this environment
resources/, databricks.yml   # Databricks Asset Bundle config — never deployed in this environment
data/                    # seed SQL, example state file, reference-only Synthea sample, knowledge-base markdown
tests/                   # one file per guardrail/concern, mocked Anthropic client, zero network calls
docs/                    # architecture.md (design rationale), flows/ (split-out flow docs), comparisons/ (pending eval plans)
```

## Build, test, lint commands

```bash
uv sync --extra dev              # install runtime + dev dependencies (uv is the ONLY supported package manager here)
uv run pytest                    # full test suite — 155 tests, ~110s, zero network calls, no ANTHROPIC_API_KEY needed
uv run pytest tests/test_x.py    # one test file
uv run ruff check .              # lint (select = E, F, I, UP; line-length 115; target-version py311)
uv run python -m ml.train_anomaly_model   # retrain the anomaly model, overwrites ml/models/vitals_anomaly.onnx
uv run python -m mcp_server.server        # run the MCP server standalone
uv run python -m simulator.producer --patients 1000 --duration 60   # requires docker compose up -d first
```

CI (`.github/workflows/ci.yml`) runs exactly `uv sync --extra dev`,
`uv run ruff check .`, `uv run pytest` on every push to `master` and every PR. Match
that before considering a change done.

## Code conventions observed in this repo

- **No agent framework, no ORM, no heavy abstraction layers.** `agent/llm.py`'s `Claude` class is a plain dataclass-adjacent wrapper around the Anthropic SDK — mirrors the sibling `abhay` project's style deliberately. Do not introduce LangChain, an ORM, or a DI framework.
- **Dependency injection over global state.** `Claude`, `MCPToolBridge`, `Tracer` are all constructor-injected into `OrchestratorAgent`, never constructed as module-level globals — this is what lets the test suite mock them with zero network calls. Follow this pattern for any new external dependency.
- **`common/contracts.py` is the single source of truth for the event schema.** `pipeline/common/schemas.py` (PySpark), `simulator/domain.py` (dataclasses), and the Pydantic models in `contracts.py` itself all describe the same wire shape from different angles and cross-check against the constants there (`tests/test_contract_consistency.py`). If you change the schema, change it in `common/contracts.py` first.
- **No-op-unless-configured pattern for every optional integration.** `agent/llm.py:Tracer` (Langfuse), `agent/otel.py` (OpenTelemetry), `agent/secrets.py:get_secret` (GCP Secret Manager), `agent/databricks_client.py:load_config` (live Databricks workspace) all follow the same shape: zero import, zero network call, and a documented fallback unless the relevant env var(s) are set. New optional integrations should follow this shape, not require configuration to avoid crashing.
- **Guardrails are enforced in code, not just requested in a system prompt.** See `docs/architecture.md`'s Guardrails table. Any new tool or dispatch path must go through the same `_dispatch` pipeline (masking → injection scan → output-size cap → audit log) in `agent/orchestrator.py` — do not add a tool that bypasses it.
- **`ToolResult(tool_use_id, content, is_error)` is the universal return shape** for every tool function, on both sides of the MCP boundary (`mcp_server/server.py:_to_call_tool_result` / `agent/mcp_bridge.py:MCPToolBridge.dispatch` round-trip it losslessly). New tools should return this shape, not raise for an expected failure.
- **Live-workspace code is tested against a mocked `httpx` transport, never a real workspace.** `DatabricksClient` takes an injectable `transport`; `tests/conftest.py` clears every `DATABRICKS_*` env var before each test so a developer's shell can't turn the suite into real network calls. A passing test there proves request/response handling only — keep its status as Stubbed until it has run against a real workspace.
- **Tests use a scripted fake `Claude`, never a real API call.** Any fake substituted for `Claude` must expose the same `run_tool_loop(system, messages, tools, dispatch, max_turns=6)` signature.
- **Docstrings carry the "why," not just the "what."** Every non-trivial module in this repo opens with a docstring explaining the design decision, not just what the code does — match that density when adding new modules, especially around anything guardrail- or masking-related.
- **Status honesty**: comments/docstrings in `pipeline/`, `governance/`, `infra/`, and `resources/` explicitly say "Databricks-only" / "not executed in this environment" / "never applied." Preserve that framing in any new file in those directories — do not imply platform code has been run when it hasn't.

## Do / don't

- **Do** run `uv run pytest` and `uv run ruff check .` before considering any change complete.
- **Do** add or update a test alongside any change to `agent/`, `common/contracts.py`, `simulator/`, or `ml/` — this repo's credibility rests on "tested, not just claimed."
- **Do** keep `common/contracts.py`, `pipeline/common/schemas.py`, and `simulator/domain.py` in sync when the event schema changes, and update `tests/test_contract_consistency.py` if needed.
- **Do** route any new DE-mode tool through `agent/mcp_bridge.py`/`mcp_server/server.py`, matching the existing seven MCP tools, unless there's a masking-adjacent reason to keep it direct-dispatch (document that reason inline the way `_dispatch_query_gold_table` does).
- **Don't** call `agent.tools.data_query.query_gold_table` results into `messages`/`ToolResult.content` without running them through `governance_guard.enforce_masking` first — this is the single most safety-critical invariant in the codebase.
- **Don't** add a tool that can drop, truncate, or destructively modify a table — `tests/test_tool_allowlist.py` asserts no such tool exists anywhere; keep it that way.
- **Don't** move, rename, or delete files under `pipeline/`, `governance/`, `infra/`, `resources/`, `databricks.yml` without updating the Databricks Asset Bundle library paths (`resources/dlt_pipeline.yml`) that reference them by relative path.
- **Don't** claim Databricks/GCP platform code (`pipeline/`, `governance/05_unity_catalog/`, `infra/terraform/`, `databricks.yml`) has been tested or executed — it hasn't, in this environment; keep status claims precise (Complete / Partial / Stubbed / Target).
- **Don't** introduce a new environment-variable-gated integration without the same no-op-unless-configured fallback used by `agent/llm.py`, `agent/otel.py`, and `agent/secrets.py`.
- **Don't** commit real secrets, API keys, or `.env` (already gitignored) — use `.env.example` as the template for new config keys.
