# Agent Bricks vs this project's own runbook search

The plan was to put an Agent Bricks Knowledge Assistant over the problem
catalog (`agent/knowledge/problem_catalog.yaml`): ask it what a problem is
and what to do, and compare it with the agent's keyword `lookup_problem`.

## What happened

On Databricks Free Edition (2026-10-15) the Knowledge Assistant API answered
but refused to create one: *"Knowledge Assistant is now a legacy feature. New
instances can no longer be created for this account. Please use Genie Agents
instead."* So the comparison couldn't be run as planned.

The same idea was built from parts the workspace does offer
(`agent/runbook_search.py`):

- 104 runbook pages generated from the catalog (`docs/runbooks/`,
  `scripts/build_runbooks.py`), uploaded to the `ops.runbooks` volume;
- embeddings from the hosted `databricks-gte-large-en` endpoint, fused with
  the keyword search by reciprocal rank;
- the top five pages handed to a chat model, which has to start its answer
  with the problem ID.

## Results

`scripts/runbook_eval.py`, Qwen 3.5 122B answering. A question is right when
the top result (or the answer's `Problem <ID>:`) is one of its expected IDs.

| Method | 30 questions | 15 held-out questions |
|---|---|---|
| Keyword lookup (`lookup_problem`) | 16 | 6 |
| Embedding search | 17 | 12 |
| Embeddings + keyword + model answer | 25 | 14 |

The held-out questions were written after the method was fixed and never
used to tune it. Per-question results:
[`results/runbook_eval_20261015.json`](results/runbook_eval_20261015.json),
[`results/runbook_eval_20261015_holdout.json`](results/runbook_eval_20261015_holdout.json).
On a laptop (Ollama: `nomic-embed-text` + `qwen3:14b`) the same pipeline got
25 and 13.

## Where it's used

`lookup_problem` ranks by this search when `RUNBOOK_SEARCH=embeddings` (or
`local` for the laptop models). Below a similarity floor (0.55 hosted, 0.64
local - between real and off-topic questions as measured) the keyword search
decides alone, so an unknown problem still gets "no catalog match, escalate".
The scheduled health check keeps keyword ranking: it acts on the top match,
and in a live run the semantic ranking put M4 above B1 for an expectation
failure.

Genie Agents, the suggested replacement, weren't tried.
