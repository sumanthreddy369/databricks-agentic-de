"""DA-mode tools: query and aggregate governed Gold tables.

Locally these run against a DuckDB file seeded from data/seed/gold_seed.sql,
built fresh if it doesn't exist yet. When a live workspace is configured
(`DATABRICKS_HOST`, `DATABRICKS_TOKEN`, `DATABRICKS_WAREHOUSE_ID` — see
agent/databricks_client.py) and no explicit local `db_path`/`seed_sql_path`
is passed, the same SQL runs against `common.contracts.GOLD_SCHEMA` on a
Databricks SQL warehouse instead (`_run`). The live path is tested only
against a mocked REST transport, never a real warehouse.

Both tools build SQL the same way:
- `table` must be in `_ALLOWED_TABLES`, and every column the model names (a
  filter, an aggregated column, a group key) must be a plain identifier. The
  model chooses all of them, so none is ever interpolated unchecked; values
  are always bound parameters.
- A masked column (`common.contracts.MASKED_COLUMNS`) can't be filtered on,
  aggregated, or grouped by. Masking redacts the value in returned rows, but
  a filter like `{"full_name": "Jane Alvarez"}` would still confirm the name
  by whether any row comes back, and a group key would print it outright.
- Results are capped at `MAX_TOOL_RESULT_ROWS + 1` rows in the query itself,
  so a large live table (e.g. `fct_vitals`) is never pulled in full just to
  be truncated afterwards; the extra row is how truncation is detected.

`query_gold_table` returns raw rows (masked by the orchestrator before the
model sees them). `aggregate_gold_table` computes counts and averages in SQL
— see the section comment above it for why both exist.
"""

import json
import os
import re
from pathlib import Path

import duckdb

from agent.databricks_client import DatabricksClient, DatabricksError, load_config
from agent.llm import ToolResult
from common.contracts import GOLD_SCHEMA, MASKED_COLUMNS, MAX_TOOL_RESULT_ROWS, MIN_CELL_SIZE

DEFAULT_DUCKDB_PATH = "data/seed/gold.duckdb"
DEFAULT_SEED_SQL_PATH = "data/seed/gold_seed.sql"

_ALLOWED_TABLES = {
    "dim_patients",
    "dim_providers",
    "fct_encounters",
    "fct_encounter_history",
    "fct_vitals",
    "gold_live_vitals_by_unit",
}

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


def _check_column(table: str, column: object, purpose: str) -> str:
    """Raises ValueError unless `column` is a plain identifier and not a
    masked column of `table`. `purpose` names the use in the error."""
    if not isinstance(column, str) or not _IDENTIFIER.match(column):
        raise ValueError(f"invalid {purpose} column: {column!r}")
    if column in MASKED_COLUMNS.get(table, set()):
        raise ValueError(f"cannot {purpose} on masked column: {column}")
    return column


def _check_value(column: str, value: object) -> object:
    if not isinstance(value, bool | int | float | str):
        raise ValueError(f"filter value for {column} must be a string, number, or boolean")
    return value


def _bind(params: list[tuple[str, object]], value: object) -> str:
    params.append((f"p{len(params)}", value))
    return f":{params[-1][0]}"


def _equality_clauses(table: str, filters: dict | None, params: list[tuple[str, object]]) -> list[str]:
    return [
        f"{_check_column(table, column, 'filter')} = {_bind(params, _check_value(column, value))}"
        for column, value in (filters or {}).items()
    ]


def _build_query(table: str, filters: dict | None, qualified_table: str) -> tuple[str, list[tuple[str, object]]]:
    """Returns (sql_with_:pN_placeholders, [(param_name, value), ...]), or
    raises ValueError with a message safe to hand back to the model."""
    params: list[tuple[str, object]] = []
    clauses = _equality_clauses(table, filters, params)
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


def _run(table: str, build, *, db_path: Path | None, seed_sql_path: Path | None) -> list[dict] | ToolResult:
    """Picks the backend (the live warehouse when configured and no explicit
    local path was passed, else DuckDB), builds the SQL against that
    backend's table name with `build(qualified_table)`, and runs it. Returns
    rows, or an error ToolResult for any expected failure."""
    config = load_config() if db_path is None and seed_sql_path is None else None
    live = config is not None and config.warehouse_id is not None
    qualified_table = f"{GOLD_SCHEMA}.{table}" if live else table

    try:
        sql, params = build(qualified_table)
    except ValueError as exc:
        return _error(str(exc))

    if live:
        try:
            return _query_live(sql, params, DatabricksClient(config))
        except DatabricksError as exc:
            return _error(f"live query failed: {exc}")

    resolved_db_path = db_path or _resolve_duckdb_path()
    resolved_seed_path = seed_sql_path or Path(os.environ.get("GOLD_SEED_SQL_PATH", DEFAULT_SEED_SQL_PATH))
    _ensure_seeded(resolved_db_path, resolved_seed_path)
    try:
        return _query_duckdb(sql, params, resolved_db_path)
    except duckdb.Error as exc:
        # e.g. a filter on a column the table doesn't have: an expected
        # model mistake, not a reason to crash the whole tool loop.
        return _error(f"query failed: {exc}")


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
    rows = _run(table, lambda qt: _build_query(table, filters, qt), db_path=db_path, seed_sql_path=seed_sql_path)
    if isinstance(rows, ToolResult):
        return rows
    return ToolResult(tool_use_id="", content=json.dumps(rows, default=str))


# --- Aggregates ---------------------------------------------------------------
#
# query_gold_table returns raw rows capped at MAX_TOOL_RESULT_ROWS, so a model
# answering "how many ICU patients?" by counting them is wrong as soon as there
# are more than 500. aggregate_gold_table computes the number in SQL instead,
# on the same allow-listed tables and backends with the same column checks.
#
# Small-cell suppression: a group covering 1-10 patients (MIN_CELL_SIZE) comes
# back without its value or its size, so a count or an average can't single
# someone out even with names masked. Group size is measured in distinct
# patients wherever the table has patient_id (50 readings from one patient are
# still one patient), and in underlying readings for the pre-aggregated
# gold_live_vitals_by_unit. dim_providers describes staff, not patients, and
# isn't suppressed.

_METRICS = {"count", "count_distinct", "avg", "min", "max", "sum"}
_COMPARISON_OPS = {"=", "!=", "<", "<=", ">", ">="}
_MAX_GROUP_BY = 3
_CELL_SIZE_SQL = {
    "dim_patients": "COUNT(DISTINCT patient_id)",
    "fct_encounters": "COUNT(DISTINCT patient_id)",
    "fct_encounter_history": "COUNT(DISTINCT patient_id)",
    "fct_vitals": "COUNT(DISTINCT patient_id)",
    "gold_live_vitals_by_unit": "SUM(reading_count)",
}
# Tables with valid_from/valid_to version bounds, where `as_of` applies.
_HISTORY_TABLES = {"fct_encounter_history"}


def _metric_sql(table: str, metric: str, column: str | None) -> str:
    if metric not in _METRICS:
        raise ValueError(f"metric must be one of {sorted(_METRICS)}")
    if metric == "count":
        return f"COUNT({_check_column(table, column, 'aggregate')})" if column else "COUNT(*)"
    if not column:
        raise ValueError(f"metric {metric} needs a column")
    col = _check_column(table, column, "aggregate")
    return f"COUNT(DISTINCT {col})" if metric == "count_distinct" else f"{metric.upper()}({col})"


def _build_aggregate(
    table: str,
    metric: str,
    column: str | None,
    filters: dict | None,
    where: list | None,
    group_by: list | None,
    as_of: str | None,
    qualified_table: str,
) -> tuple[str, list[tuple[str, object]]]:
    metric_sql = _metric_sql(table, metric, column)
    group_cols = [_check_column(table, g, "group by") for g in (group_by or [])]
    if len(group_cols) > _MAX_GROUP_BY:
        raise ValueError(f"group_by takes at most {_MAX_GROUP_BY} columns")

    params: list[tuple[str, object]] = []
    clauses = _equality_clauses(table, filters, params)
    for condition in where or []:
        if not isinstance(condition, dict):
            raise ValueError("each where condition must be an object with column, op, value")
        col = _check_column(table, condition.get("column"), "filter")
        op = condition.get("op")
        if op not in _COMPARISON_OPS:
            raise ValueError(f"op must be one of {sorted(_COMPARISON_OPS)}")
        clauses.append(f"{col} {op} {_bind(params, _check_value(col, condition.get('value')))}")
    if as_of is not None:
        if table not in _HISTORY_TABLES:
            raise ValueError(f"as_of only applies to {sorted(_HISTORY_TABLES)}")
        ts = f"CAST({_bind(params, _check_value('as_of', as_of))} AS TIMESTAMP)"
        clauses.append(f"valid_from <= {ts} AND (valid_to IS NULL OR valid_to > {ts})")

    select = [*group_cols, f"{metric_sql} AS value"]
    if table in _CELL_SIZE_SQL:
        select.append(f"{_CELL_SIZE_SQL[table]} AS _cell_size")
    sql = f"SELECT {', '.join(select)} FROM {qualified_table}"  # noqa: S608 - every identifier is validated above
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    if group_cols:
        sql += f" GROUP BY {', '.join(group_cols)} ORDER BY {', '.join(group_cols)}"
    sql += f" LIMIT {MAX_TOOL_RESULT_ROWS + 1}"
    return sql, params


def aggregate_gold_table(
    table: str,
    metric: str,
    column: str | None = None,
    filters: dict | None = None,
    where: list | None = None,
    group_by: list | None = None,
    as_of: str | None = None,
    *,
    db_path: Path | None = None,
    seed_sql_path: Path | None = None,
) -> ToolResult:
    """Computes `metric` over a Gold table in SQL. Content is a JSON object:
    {"table", "metric", "column", "group_by", "as_of", "rows": [...],
    "suppressed_groups", "min_cell_size", "truncated"}. Each row holds the
    group-by values and "value"; a suppressed row has "value": null and
    "suppressed": true, and never reveals its size."""
    if table not in _ALLOWED_TABLES:
        return _error(f"unknown table: {table}")
    rows = _run(
        table,
        lambda qt: _build_aggregate(table, metric, column, filters, where, group_by, as_of, qt),
        db_path=db_path,
        seed_sql_path=seed_sql_path,
    )
    if isinstance(rows, ToolResult):
        return rows

    out, suppressed = [], 0
    for row in rows[:MAX_TOOL_RESULT_ROWS]:
        cell_size = row.pop("_cell_size", None)
        if cell_size is not None and 0 < cell_size < MIN_CELL_SIZE:
            row = {**row, "value": None, "suppressed": True}
            suppressed += 1
        out.append(row)
    payload = {
        "table": table,
        "metric": metric,
        "column": column,
        "group_by": group_by or [],
        "as_of": as_of,
        "rows": out,
        "suppressed_groups": suppressed,
        "min_cell_size": MIN_CELL_SIZE,
        "truncated": len(rows) > MAX_TOOL_RESULT_ROWS,
    }
    return ToolResult(tool_use_id="", content=json.dumps(payload, default=str))
