# Agent Bricks vs. this project's custom orchestrator agent

Databricks Agent Bricks is the platform's managed, declarative way to build
and deploy an agent; tool/knowledge-source wiring, evaluation and serving
are handled by the platform. In this project's direction (`docs/plan.md`),
platform agents do the work and this repo is the glue-and-guardrail layer
around them. So the question here is **which parts of that layer the
platform can take over**, not whether to use Agent Bricks at all.

Two concrete uses are planned:

1. **A runbook / how-to agent:** an Agent Bricks knowledge agent over the
   real-time problem catalog (`agent/knowledge/problem_catalog.yaml`). This
   could replace or back our deterministic `lookup_problem` matcher.
2. **Hosting the router itself:** running the orchestrator's routing on
   Agent Bricks instead of our own tool loop (`agent/llm.py`,
   `agent/orchestrator.py`). Our guardrails, approval gate and audit log
   stay as code that wraps whatever does the routing.

This document is an evaluation plan, not a completed comparison. No Agent
Bricks deployment has been built or measured for this project.

## Why this comparison is worth doing

Every piece of the layer the platform can run for us is less code to own,
which is the point of buying before building. What has to stay ours is
whatever the platform can't guarantee in a way we can test: masking before
the model, the approval gate, the audit trail. Measuring both uses above
shows where that line actually is.

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
