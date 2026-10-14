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

## Status: Pending (setup done, comparison not yet run)

The comparison itself has **not** been run: no question set, no scores, no
"X is better than Y" conclusions exist for any row in the table above.

What exists now (2026-10-14):

- A Genie space over the six Gold tables in the GCP trial workspace,
  defined in `governance/genie/space.json`. Its instructions carry the
  project's metric definitions and governance rules.
- The guardrail layer (`agent/tools/genie.py`) has been run against that
  real space, not just mocks.

### First observations (3 questions - not the comparison)

| Question | Genie answer | Direct SQL on Gold | Latency | Guardrail result |
|---|---|---|---|---|
| How many patients are currently in the ICU? | 34 | 34 | 15 s | Passed (SQL inside Gold; no PHI; no small groups) |
| What is the full name of patient pt_00052? | - | - | 0 s | Refused by our layer; never sent to Genie |
| What is the current census by unit? | Cardiology 35, ED 52, ICU 34, MedSurg 39, Oncology 40 | Same | 18 s | Passed |

Definition drift seen on the first question: Genie filtered
`unit ILIKE '%ICU%'` rather than `unit = 'ICU'`. Same answer on this data,
but it would also count units like PICU or NICU in a real hospital. That's
the case for a shared metrics layer (problem catalog F1/F9), not a verdict
on Genie.

Three hand-picked questions can't support any conclusion about accuracy,
cost, or governance. Those need the golden question set and the measurement
method above.

### After the metrics layer (same day)

The space's instructions and example SQL are now generated from
`agent/knowledge/metrics.yaml`, the definitions `compute_metric` also uses.

| Question | Genie SQL filter | Genie answer | compute_metric (live) |
|---|---|---|---|
| How many patients are currently in the ICU? | `unit = 'ICU'` (was `ILIKE '%ICU%'`) | 34 | 34 |
| How many patients are in intensive care right now? | `unit = 'ICU'` | 34 | 34 (normalized "intensive care" -> ICU) |
| How many patients are currently in the PICU? | no query; listed the allowed units | - | refused with the allowed units |

Latency on these: Genie 7-24 s, compute_metric about 2 s against the same
warehouse. Still three questions, not the comparison.

