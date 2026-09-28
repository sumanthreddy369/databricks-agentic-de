"""Databricks Vector Search-backed clinical-knowledge lookup.

TIER 2 SCAFFOLD — structurally correct, NOT exercised against a live
Databricks workspace or a real Vector Search index in this environment (no
workspace connection exists here). Do not read a successful-looking response
shape in this file's docstrings/comments as evidence this was tested against
real infrastructure — see `docs/comparisons/`'s "## Status: Pending" sections
and README.md's Status table for the same distinction applied project-wide.

Real-deployment shape: `data/knowledge/clinical_protocols.md`'s entries would
be chunked and embedded into a Databricks Vector Search index (e.g. over a
Delta table populated from that markdown file), and this function would call
`databricks.vector_search.client.VectorSearchClient` to query that index.

Same no-op-unless-configured pattern used throughout this project
(`agent/llm.py:Tracer`, `agent/secrets.py:get_secret`, `agent/otel.py`):
without `DATABRICKS_HOST`/`DATABRICKS_TOKEN`/`VECTOR_SEARCH_ENDPOINT` all
set, this returns a clear "not configured" result and makes zero network
calls — it never crashes a caller just because a live workspace isn't wired
up yet (which, per this project's Status docs, it isn't, here).
"""

import json
import os

from agent.llm import ToolResult

DEFAULT_INDEX_NAME = "healthcare_agentic_de.gold.clinical_protocols_index"
NUM_RESULTS = 5


def _is_configured(endpoint_name: str | None) -> bool:
    return bool(endpoint_name and os.environ.get("DATABRICKS_HOST") and os.environ.get("DATABRICKS_TOKEN"))


def search_clinical_knowledge(
    query: str,
    *,
    endpoint_name: str | None = None,
    index_name: str | None = None,
) -> ToolResult:
    """Searches the clinical-knowledge Vector Search index for `query`.

    Returns a ToolResult whose content is always valid JSON:
    - `{"ok": false, "configured": false, ...}` (never an error — a genuinely
      absent configuration is an expected, everyday state for this project,
      not a failure) when Databricks Vector Search isn't configured, or the
      `databricks-vectorsearch` package (the `databricks` extra) isn't
      installed.
    - `{"ok": true, "results": [...]}` on a real, successful query.
    - `{"ok": false, "error": "..."}`, `is_error=True`, if a REAL,
      configured Vector Search call fails (a genuine runtime error, not a
      configuration gap).
    """
    resolved_endpoint = endpoint_name or os.environ.get("VECTOR_SEARCH_ENDPOINT")
    resolved_index = index_name or os.environ.get("VECTOR_SEARCH_INDEX", DEFAULT_INDEX_NAME)

    if not _is_configured(resolved_endpoint):
        return ToolResult(
            tool_use_id="",
            content=json.dumps(
                {
                    "ok": False,
                    "configured": False,
                    "message": (
                        "Databricks Vector Search is not configured (need DATABRICKS_HOST, "
                        "DATABRICKS_TOKEN, and VECTOR_SEARCH_ENDPOINT) — this is a Tier 2 scaffold "
                        "not yet exercised against a live workspace in this environment. "
                        "See docs/comparisons/ and README.md's Status section."
                    ),
                }
            ),
        )

    try:
        from databricks.vector_search.client import VectorSearchClient
    except ImportError:
        return ToolResult(
            tool_use_id="",
            content=json.dumps(
                {
                    "ok": False,
                    "configured": False,
                    "message": (
                        "databricks-vectorsearch is not installed (it's part of this project's "
                        "optional 'databricks' extra — `uv sync --extra databricks`)."
                    ),
                }
            ),
        )

    try:
        client = VectorSearchClient()
        index = client.get_index(endpoint_name=resolved_endpoint, index_name=resolved_index)
        raw_results = index.similarity_search(query_text=query, columns=["content"], num_results=NUM_RESULTS)
        return ToolResult(tool_use_id="", content=json.dumps({"ok": True, "results": raw_results}))
    except Exception as exc:
        # A REAL, configured Vector Search call that failed at runtime (bad
        # index name, auth expired, service outage) — unlike the "not
        # configured" case above, this genuinely is an error.
        return ToolResult(
            tool_use_id="",
            content=json.dumps({"ok": False, "error": str(exc)}),
            is_error=True,
        )
