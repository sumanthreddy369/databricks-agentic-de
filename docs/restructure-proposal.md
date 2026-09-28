# Restructure proposal (not applied)

Written while documenting this repository (see `README.md`,
`docs/architecture.md`). This is a set of suggestions for you to review and
approve — nothing in this file has been acted on, and no source file was
moved, renamed, or deleted as part of this documentation pass.

## 1. `tests/` is flat and will keep growing

24 files sit directly under `tests/` with no subdirectory. It's still
readable today because filenames are descriptive
(`test_masking_guard.py`, `test_escalation_ceiling.py`), but every future
guardrail or tool adds another top-level file. A light grouping —
`tests/agent/`, `tests/simulator/`, `tests/ml/`, `tests/mcp/` — would mirror
the top-level package layout without changing how `pytest` discovers
anything (`testpaths = ["tests"]` in `pyproject.toml` already recurses).
Low priority: the current flat layout is not causing confusion yet.

## 2. `mcp_server/` could live under `agent/`

`mcp_server/server.py` only exists to expose `agent/tools/*` over MCP for
`agent/mcp_bridge.py` to call — it has no independent existence from the
agent. Moving it to `agent/mcp_server/` would make the ownership
relationship visible in the path, matching how `agent/tools/` and
`agent/mcp_bridge.py` already sit together. Counter-argument: keeping it as
a top-level package makes `python -m mcp_server.server` and the "any
MCP-speaking client can point at this independently" story (see
`docs/architecture.md`'s MCP section) slightly more visually obvious as a
standalone server. Judgment call either way — no strong recommendation.

## 3. `docs/comparisons/` naming vs. status

All three files in `docs/comparisons/` are evaluation *plans*, not
comparisons (each is explicitly `## Status: Pending`, with zero results). A
reader skimming the directory name before opening a file could reasonably
expect completed comparisons. Renaming to `docs/comparison_plans/` (or
adding a one-line `docs/comparisons/README.md` stating the pending status
up front) would remove that ambiguity. Low priority — each file's own
"Status: Pending" section already makes this clear on open, and `README.md`
now states it plainly at every link site.

## 4. `data/state/` mixes a template with gitignored runtime output

`data/state/pipeline_state.example.json` (tracked, a template) and the
real `pipeline_state.json`/`audit_log.jsonl` (gitignored runtime output)
live in the same directory, distinguished only by the `.gitignore` entries
and the `.example` suffix. This works fine today. If this project grows
more runtime-state files, splitting into `data/state/examples/` (tracked)
vs. leaving the gitignored runtime output at `data/state/` would make the
tracked/untracked boundary visible from the directory listing alone, not
just from `.gitignore`. Low priority.

## Not proposed

Everything else — the `01_ingest`/`02_bronze`/`03_silver`/`04_gold`
numbering in `pipeline/`, the `agent/tools/` flat-file layout, keeping
`common/contracts.py` as the single schema source of truth, and the overall
`pipeline/` vs. `governance/` vs. `infra/` vs. `resources/` top-level split
— is working well and is not suggested for change. The numbered pipeline
stages in particular make the medallion flow legible from `ls` output
alone, which is a genuine strength worth keeping.
