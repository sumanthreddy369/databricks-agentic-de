"""DA-mode tool: query governed Gold tables.

Locally this runs against a DuckDB file seeded from data/seed/gold_seed.sql,
built fresh if it doesn't exist yet. In a real deployment, the swap point is
exactly the query-execution block below — replace the DuckDB connection with
`databricks_sql_connector.connect(...)` and execute the same SQL against the
real Gold schema; the function signature (`query_gold_table(table, filters)`)
stays identical, so agent/orchestrator.py needs no changes.
"""

import os
from pathlib import Path

import duckdb

from agent.llm import ToolResult

DEFAULT_DUCKDB_PATH = "data/seed/gold.duckdb"
DEFAULT_SEED_SQL_PATH = "data/seed/gold_seed.sql"

_ALLOWED_TABLES = {"dim_patients", "dim_providers", "fct_encounters", "fct_vitals", "gold_live_vitals_by_unit"}


def _resolve_duckdb_path() -> Path:
    return Path(os.environ.get("GOLD_DUCKDB_PATH", DEFAULT_DUCKDB_PATH))


def _ensure_seeded(db_path: Path, seed_sql_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        return
    seed_sql = Path(seed_sql_path).read_text()
    conn = duckdb.connect(str(db_path))
    try:
        conn.execute(seed_sql)
    finally:
        conn.close()


def query_gold_table(
    table: str,
    filters: dict | None = None,
    *,
    db_path: Path | None = None,
    seed_sql_path: Path | None = None,
) -> ToolResult:
    """Returns rows from a Gold table as a ToolResult whose content is a JSON
    list of row dicts. `filters` is a simple {column: value} equality map.

    NOTE: the caller (agent/orchestrator.py's dispatch()) is responsible for
    running the returned rows through governance_guard.enforce_masking()
    before this ever becomes part of a Claude message — this function itself
    returns raw rows and does no masking, by design, so there is exactly one
    place in the codebase that's allowed to skip masking (never) and exactly
    one place that's required to apply it (orchestrator.dispatch).
    """
    import json

    if table not in _ALLOWED_TABLES:
        return ToolResult(tool_use_id="", content=json.dumps({"error": f"unknown table: {table}"}), is_error=True)

    resolved_db_path = db_path or _resolve_duckdb_path()
    resolved_seed_path = seed_sql_path or Path(os.environ.get("GOLD_SEED_SQL_PATH", DEFAULT_SEED_SQL_PATH))
    _ensure_seeded(resolved_db_path, resolved_seed_path)

    conn = duckdb.connect(str(resolved_db_path))
    try:
        query = f"SELECT * FROM {table}"  # noqa: S608 - table is validated against an allow-list above
        params = []
        if filters:
            clauses = []
            for column, value in filters.items():
                clauses.append(f"{column} = ?")
                params.append(value)
            query += " WHERE " + " AND ".join(clauses)
        cursor = conn.execute(query, params)
        columns = [col[0] for col in cursor.description]
        rows = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
    finally:
        conn.close()

    return ToolResult(tool_use_id="", content=json.dumps(rows, default=str))
