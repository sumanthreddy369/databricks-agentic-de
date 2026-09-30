"""DA-mode tool: query governed Gold tables.

Locally this runs against a DuckDB file seeded from data/seed/gold_seed.sql,
built fresh if it doesn't exist yet. When a live workspace is configured
(`DATABRICKS_HOST`, `DATABRICKS_TOKEN`, `DATABRICKS_WAREHOUSE_ID` — see
agent/databricks_client.py) and no explicit local `db_path`/`seed_sql_path`
is passed, the same query runs against `common.contracts.GOLD_SCHEMA` on a
Databricks SQL warehouse instead. The live path is tested only against a
mocked REST transport, never a real warehouse. The function signature
(`query_gold_table(table, filters)`) is identical either way, so
agent/orchestrator.py needs no changes.

Both backends build SQL the same way, from `_build_query`:
- `table` must be in `_ALLOWED_TABLES`, and every filter key must be a plain
  identifier. The model chooses both, so neither is ever interpolated
  unchecked; filter values are always bound parameters.
- Filtering on a masked column (`common.contracts.MASKED_COLUMNS`) is
  refused. Masking redacts the value in the returned rows, but a filter like
  `{"full_name": "Jane Alvarez"}` would still confirm the name by whether any
  row comes back. Refusing it here closes that path for both backends.
- Results are capped at `MAX_TOOL_RESULT_ROWS + 1` rows in the query itself,
  so a large live table (e.g. `fct_vitals`) is never pulled in full just to
  be truncated by the orchestrator afterwards; the extra row lets the
  orchestrator still see, and log, that truncation happened.
"""

import json
import os
import re
from pathlib import Path

import duckdb

from agent.databricks_client import DatabricksClient, DatabricksError, load_config
from agent.llm import ToolResult
from common.contracts import GOLD_SCHEMA, MASKED_COLUMNS, MAX_TOOL_RESULT_ROWS

DEFAULT_DUCKDB_PATH = "data/seed/gold.duckdb"
DEFAULT_SEED_SQL_PATH = "data/seed/gold_seed.sql"

_ALLOWED_TABLES = {"dim_patients", "dim_providers", "fct_encounters", "fct_vitals", "gold_live_vitals_by_unit"}

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Python type -> Databricks SQL parameter type. bool is checked before int,
# since bool is a subclass of int.
_PARAM_TYPES = ((bool, "BOOLEAN"), (int, "BIGINT"), (float, "DOUBLE"), (str, "STRING"))


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


def _error(message: str) -> ToolResult:
    return ToolResult(tool_use_id="", content=json.dumps({"error": message}), is_error=True)


def _build_query(table: str, filters: dict | None, qualified_table: str) -> tuple[str, list[tuple[str, object]]]:
    """Returns (sql_with_:pN_placeholders, [(param_name, value), ...]), or
    raises ValueError with a message safe to hand back to the model."""
    clauses, params = [], []
    for i, (column, value) in enumerate((filters or {}).items()):
        if not isinstance(column, str) or not _IDENTIFIER.match(column):
            raise ValueError(f"invalid filter column: {column!r}")
        if column in MASKED_COLUMNS.get(table, set()):
            raise ValueError(f"cannot filter on masked column: {column}")
        if not isinstance(value, bool | int | float | str):
            raise ValueError(f"filter value for {column} must be a string, number, or boolean")
        clauses.append(f"{column} = :p{i}")
        params.append((f"p{i}", value))
    sql = f"SELECT * FROM {qualified_table}"  # noqa: S608 - table is allow-listed, columns are validated
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += f" LIMIT {MAX_TOOL_RESULT_ROWS + 1}"
    return sql, params


def _query_duckdb(sql: str, params: list[tuple[str, object]], db_path: Path) -> list[dict]:
    # DuckDB takes `$name` placeholders with a dict of named parameters.
    conn = duckdb.connect(str(db_path))
    try:
        cursor = conn.execute(re.sub(r":(p\d+)", r"$\1", sql), dict(params))
        columns = [col[0] for col in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
    finally:
        conn.close()


def _query_live(sql: str, params: list[tuple[str, object]], client: DatabricksClient) -> list[dict]:
    parameters = []
    for name, value in params:
        type_name = next(t for py_type, t in _PARAM_TYPES if isinstance(value, py_type))
        rendered = str(value).lower() if isinstance(value, bool) else str(value)
        parameters.append({"name": name, "value": rendered, "type": type_name})
    return client.execute_statement(sql, parameters)


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
    one place that's required to apply it (orchestrator.dispatch). On the
    live path, Unity Catalog's own column masks have already been applied by
    the warehouse too; enforce_masking still runs regardless.
    """
    if table not in _ALLOWED_TABLES:
        return _error(f"unknown table: {table}")

    config = load_config() if db_path is None and seed_sql_path is None else None
    live = config is not None and config.warehouse_id is not None
    qualified_table = f"{GOLD_SCHEMA}.{table}" if live else table

    try:
        sql, params = _build_query(table, filters, qualified_table)
    except ValueError as exc:
        return _error(str(exc))

    if live:
        try:
            rows = _query_live(sql, params, DatabricksClient(config))
        except DatabricksError as exc:
            return _error(f"live query failed: {exc}")
    else:
        resolved_db_path = db_path or _resolve_duckdb_path()
        resolved_seed_path = seed_sql_path or Path(os.environ.get("GOLD_SEED_SQL_PATH", DEFAULT_SEED_SQL_PATH))
        _ensure_seeded(resolved_db_path, resolved_seed_path)
        try:
            rows = _query_duckdb(sql, params, resolved_db_path)
        except duckdb.Error as exc:
            # e.g. a filter on a column the table doesn't have: an expected
            # model mistake, not a reason to crash the whole tool loop.
            return _error(f"query failed: {exc}")

    return ToolResult(tool_use_id="", content=json.dumps(rows, default=str))
