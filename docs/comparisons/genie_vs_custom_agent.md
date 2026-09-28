# Genie vs. this project's custom orchestrator agent

This project's DA-mode Q&A path (`agent/orchestrator.py` + `agent/tools/data_query.py`)
is a hand-built Claude tool-loop agent, not Databricks Genie (the platform's
own no-code/low-code natural-language-to-SQL assistant over Unity Catalog
tables). That was a deliberate choice for this portfolio project — see
`README.md`'s framing ("this project takes the harder, more differentiated
path... one hand-built orchestrator agent — not a vendor no-code tool") — but
it's worth being honest about what that choice actually costs and buys,
rather than asserting the custom path is strictly better. This document is an
evaluation plan for that comparison, not its result.

## Why this comparison is worth doing

Genie is the platform-native answer to exactly the same job DA-mode does
(governed natural-language querying over Gold tables), so it's the most
direct apples-to-apples comparison available for this project's core
differentiator. A hiring manager or reviewer evaluating "why build this by
hand instead of using the vendor tool" deserves a real, tested answer, not
just an architectural argument.

## Evaluation criteria

| Criterion | What "good" looks like | How it would be measured |
|---|---|---|
| Setup time | Time from zero to a working, governed Q&A surface over `gold` | Wall-clock time to first correct answer, both paths, same Gold schema |
| Governance/masking enforcement | PHI masking and prompt-injection defense are architecturally guaranteed, not just configured | Attempt the same adversarial prompts (`simulator/chaos.build_prompt_injection_payload`) against both; check whether masked values ever appear in either path's response/trace |
| Answer latency | Time from question to answer, p50/p95 | Same fixed set of benchmark questions run against both, wall-clock timed |
| Cost | $/1000 queries, including any platform/DBU premium for Genie vs. Claude API token cost for the custom agent | Actual billing/usage data from a real run of both, over the same query set |
| Ability to unit-test | Can the guardrails be proven in a CI pipeline with zero live workspace, zero network calls? | This project's own `tests/test_masking_guard.py`/`test_prompt_injection_guard.py` pattern attempted against Genie (likely: no, Genie is workspace-hosted and not something you can drive from a mocked local test) |
| Vendor lock-in | How much of the implementation is portable to a non-Databricks lakehouse | Genie: fully Databricks-proprietary. Custom agent: the Claude tool-loop, masking, and injection-defense code is platform-agnostic (already proven by this project's DuckDB local stand-in for a real Gold connection) |

## Status: Pending

This comparison has **not** been run. It requires a real, GCP-connected
Databricks workspace with Genie enabled and configured over the same `gold`
schema this project's `data/seed/gold_seed.sql` seeds locally — that
workspace was still being set up as of this task and was not yet live in
this environment. No scores, benchmarks, or "X is better than Y" conclusions
exist yet for any row in the table above; do not treat this document as
containing results. It will be filled in with real, measured numbers once
that workspace is available.
