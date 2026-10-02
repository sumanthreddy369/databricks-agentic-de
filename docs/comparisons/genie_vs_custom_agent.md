# Genie: alone, behind our guardrails, and against our fallback

Genie (Databricks' natural-language-to-SQL agent over Unity Catalog) is the
**primary** way this project answers analyst questions. DA mode routes
questions to it through `agent/tools/genie.py`. This project doesn't build a
replacement for Genie. It builds the glue and guardrails around it, plus a
fallback (`aggregate_gold_table` / `query_gold_table`) for when Genie isn't
available.

So the comparison measures three configurations on the same questions:

1. **Genie on its own**, as a team would use it straight from the platform.
2. **Genie behind this layer** (`ask_genie`): PHI-request refusal, SQL
   schema check, masking backstop, small-count suppression, audit.
3. **The fallback agent** (no Genie): the baseline, and what runs when Genie
   is down or not configured.

This document is an evaluation plan for that comparison, not its result.

## Why this comparison is worth doing

It answers the question every team adopting a platform agent has to answer
before automating more: **how much does the guardrail layer add, and what
does it cost?** For example: does it catch leaks or out-of-scope SQL that
Genie alone lets through, does it block answers it shouldn't, and how much
latency does it add? Measured numbers here are what justify the layer, and
what would justify trusting Genie with more.

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
