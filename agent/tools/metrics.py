"""The metrics layer: one definition of each business metric, used by both
the fallback agent and Genie.

agent/knowledge/metrics.yaml defines every metric (table, measure, fixed
filters, allowed dimensions) and every dimension (closed list of values,
synonyms). This module turns those definitions into:

- `compute_metric` - the fallback agent's tool. It answers by metric name, so
  the agent never decides on its own what "current census" means. It runs
  through `aggregate_gold_table`, so masking, small-cell suppression, bound
  parameters and size caps all still apply.
- `render_sql` - the same computation as plain Databricks SQL, which
  scripts/build_genie_space.py puts into Genie's example SQL and
  instructions. tests/test_metrics_layer.py runs both on the same data and
  requires identical numbers.

Dimension values are matched exactly against the closed list, after
normalizing synonyms and case ("intensive care" -> "ICU"). An unknown value
is refused with the allowed list rather than widened into a pattern match -
the drift seen on the first real Genie run (`unit ILIKE '%ICU%'`, which would
also count a PICU or NICU).
"""

import json
from functools import lru_cache
from pathlib import Path

import yaml

from agent.llm import ToolResult
from agent.tools import data_query
from common.contracts import GOLD_SCHEMA

METRICS_PATH = Path(__file__).resolve().parent.parent / "knowledge" / "metrics.yaml"


@lru_cache(maxsize=1)
def load() -> dict:
    data = yaml.safe_load(METRICS_PATH.read_text(encoding="utf-8"))
    return {"dimensions": data["dimensions"], "metrics": {m["id"]: m for m in data["metrics"]}}


def _error(message: str) -> ToolResult:
    return ToolResult(tool_use_id="", content=json.dumps({"error": message}), is_error=True)


def normalize(dimension: str, value: object, table: str) -> str:
    """Canonical value for a dimension, or ValueError naming the allowed ones."""
    spec = load()["dimensions"][dimension]
    allowed = list(spec.get("values", [])) + list(spec.get("extra_values", {}).get(table, []))
    if not allowed:  # free-form dimension, e.g. a timestamp
        return value
    text = str(value).strip()
    synonym = spec.get("synonyms", {}).get(text.lower())
    if synonym:
        return synonym
    for candidate in allowed:
        if candidate.lower() == text.lower():
            return candidate
    raise ValueError(f"unknown {dimension} {value!r}; allowed values: {allowed}")


def _resolve(metric_id: str, group_by, filters, since, until, as_of):
    """Validates a request against the metric's definition and returns the
    aggregate_gold_table arguments it means."""
    metrics = load()["metrics"]
    if metric_id not in metrics:
        raise ValueError(f"unknown metric {metric_id!r}; defined metrics: {sorted(metrics)}")
    m = metrics[metric_id]
    allowed_dims = m.get("dimensions", [])

    for dim in group_by or []:
        if dim not in allowed_dims:
            raise ValueError(f"{metric_id} can be grouped by {allowed_dims}, not {dim!r}")
    merged = dict(m.get("filters", {}))
    for dim, value in (filters or {}).items():
        if dim not in allowed_dims:
            raise ValueError(f"{metric_id} can be filtered by {allowed_dims}, not {dim!r}")
        merged[dim] = normalize(dim, value, m["table"])

    if m.get("requires_as_of") and not as_of:
        raise ValueError(f"{metric_id} needs as_of (the point in time to measure at)")
    if as_of and not m.get("requires_as_of"):
        raise ValueError(f"{metric_id} is current-state; use census_at_time for past points in time")
    where = []
    if since or until:
        column = m.get("time_column")
        if not column:
            raise ValueError(f"{metric_id} has no time column to filter on")
        if since:
            where.append({"column": column, "op": ">=", "value": since})
        if until:
            where.append({"column": column, "op": "<", "value": until})

    measure = m["measure"]
    return {
        "table": m["table"],
        "metric": measure["type"],
        "column": measure.get("column"),
        "weight": measure.get("weight"),
        "filters": merged or None,
        "where": where or None,
        "group_by": list(group_by or []) or None,
        "as_of": as_of,
    }, m


def compute_metric(
    metric: str,
    group_by: list | None = None,
    filters: dict | None = None,
    since: str | None = None,
    until: str | None = None,
    as_of: str | None = None,
    *,
    db_path: Path | None = None,
    seed_sql_path: Path | None = None,
) -> ToolResult:
    try:
        args, definition = _resolve(metric, group_by, filters, since, until, as_of)
    except ValueError as exc:
        return _error(str(exc))
    result = data_query.aggregate_gold_table(
        **args,
        db_path=Path(db_path) if db_path else None,
        seed_sql_path=Path(seed_sql_path) if seed_sql_path else None,
    )
    if result.is_error:
        return result
    payload = json.loads(result.content)
    payload["metric_id"] = metric
    payload["definition"] = {"name": definition["name"], "description": definition["description"].strip()}
    payload["applied_filters"] = args["filters"] or {}
    return ToolResult(tool_use_id="", content=json.dumps(payload, default=str))


def _literal(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def render_sql(
    metric: str,
    group_by: list | None = None,
    filters: dict | None = None,
    as_of: str | None = None,
    *,
    qualified: bool = True,
) -> str:
    """The metric as plain SQL (literals inlined) for Genie's example SQL.
    Same definition as compute_metric; small-cell suppression is applied by
    our guardrail layer on Genie's results, not in this SQL."""
    args, _ = _resolve(metric, group_by, filters, None, None, as_of)
    measure = load()["metrics"][metric]["measure"]
    col, kind = measure.get("column"), measure["type"]
    if kind == "count":
        expr = "count(*)"
    elif kind == "count_distinct":
        expr = f"count(DISTINCT {col})"
    elif kind == "weighted_avg":
        w = measure["weight"]
        expr = f"sum({col} * {w}) / nullif(sum({w}), 0)"
    else:
        expr = f"{kind}({col})"
    table = f"{GOLD_SCHEMA}.{args['table']}" if qualified else args["table"]
    groups = args["group_by"] or []
    lines = [f"SELECT {', '.join([*groups, f'{expr} AS value'])}", f"FROM {table}"]
    conditions = [f"{k} = {_literal(v)}" for k, v in (args["filters"] or {}).items()]
    if as_of:
        ts = f"TIMESTAMP {_literal(as_of)}"
        conditions.append(f"valid_from <= {ts} AND (valid_to IS NULL OR valid_to > {ts})")
    if conditions:
        lines.append("WHERE " + "\n  AND ".join(conditions))
    if groups:
        lines += [f"GROUP BY {', '.join(groups)}", f"ORDER BY {', '.join(groups)}"]
    return "\n".join(lines)
