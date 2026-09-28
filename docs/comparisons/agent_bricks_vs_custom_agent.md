# Agent Bricks vs. this project's custom orchestrator agent

Databricks Agent Bricks is the platform's managed, declarative way to build
and deploy an agent (tool/knowledge-source wiring, evaluation, and serving
handled by the platform). This project instead hand-builds the whole tool
loop (`agent/llm.py:Claude.run_tool_loop`, `agent/orchestrator.py`) directly
against the Anthropic SDK, now also wrapping the DE-mode tools in a real MCP
server (`mcp_server/server.py`) rather than a platform-specific agent
framework. This document is an evaluation plan for comparing the two
approaches, not a completed comparison — no Agent Bricks deployment has been
built or measured for this project.

## Why this comparison is worth doing

Agent Bricks is the most direct platform-native alternative to this entire
project's agent layer (DE-mode + DA-mode combined, not just the Q&A half
Genie would compare against). Understanding the real trade-off — managed
deployment/evaluation convenience vs. full control over guardrail internals
and unit-testability — is directly relevant to whether a real production
version of this project should ever migrate off the hand-built path.

## Evaluation criteria

| Criterion | What "good" looks like | How it would be measured |
|---|---|---|
| Setup time | Time to a deployed, working agent with the same DE-mode + DA-mode tool set | Wall-clock time, both paths, same tool functions (`agent/tools/pipeline_health.py`, `agent/tools/data_query.py`) |
| Governance/masking enforcement | The masking-before-messages guarantee (`agent/orchestrator.py:_dispatch_query_gold_table`) holds regardless of platform | Whether Agent Bricks' tool-calling layer allows the same "mask before the LLM ever sees a raw row" architectural guarantee, or only a prompt-level instruction |
| Answer latency | Time from request to final answer/action, p50/p95 | Same fixed DE-mode and DA-mode scenario set, timed on both |
| Cost | $/1000 requests, including Agent Bricks' platform overhead vs. this project's direct Claude API token cost | Actual billing data from a real run of both |
| Ability to unit-test | Can the escalation-ceiling/kill-switch/audit-trail guardrails (see `docs/architecture.md`'s Guardrails table) be proven with a mocked LLM client and zero network calls, the way `tests/` does today? | Attempt the equivalent of this project's `tests/test_escalation_ceiling.py`/`test_kill_switch.py` against an Agent Bricks-deployed agent |
| Vendor lock-in | Portability of the tool implementations themselves off Databricks | This project's tools already run against a local JSON state file / DuckDB stand-in with no Databricks dependency at all; Agent Bricks' deployment/orchestration layer is Databricks-proprietary by definition |

## Status: Pending

This comparison has **not** been run. It requires a real, GCP-connected
Databricks workspace with Agent Bricks enabled, wired to the same
`agent/tools/pipeline_health.py`/`agent/tools/data_query.py` functions (or
their real-workspace equivalents) this project's custom orchestrator already
uses — that workspace was still being set up as of this task and was not yet
live in this environment. No scores, benchmarks, or "X is better than Y"
conclusions exist yet for any row in the table above; do not treat this
document as containing results. It will be filled in with real, measured
numbers once that workspace is available.
