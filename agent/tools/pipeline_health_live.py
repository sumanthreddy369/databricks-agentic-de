"""Live-workspace implementations of the DE-mode pipeline-health tools.

STATUS: tested only against a mocked Databricks REST transport
(tests/test_pipeline_health_live.py); never run against a real workspace in
this environment.

`agent/tools/pipeline_health.py` delegates here when `live_backend()`
returns a client, i.e. when `DATABRICKS_HOST`, `DATABRICKS_TOKEN`, and
`DATABRICKS_PIPELINE_IDS` are all set (see agent/databricks_client.py).
Every payload keeps the same shape as its local state-file counterpart, so
the orchestrator, its guardrails, the MCP server, and the DE system prompt
don't change.

Two tools have no honest live equivalent and return an explicit error in
live mode instead of pretending to act:
- `detect_schema_drift`: the Kafka topic's schema isn't observable through
  a Databricks API, and the agent's grants don't reach Bronze, where
  malformed payloads land in `_corrupt_record`.
- `quarantine_bad_records`: DLT has no "quarantine these rows" API.
  `expect_or_drop` rows are already dropped by the pipeline itself; clearing
  anything else would take a code or upstream-data fix, or a full refresh,
  which is destructive and out of bounds.
The error message itself tells the agent to escalate rather than retry
(`unsupported` below); the quarantine path also can't loop, since an error
result never increments the escalation-ceiling counter.

`notify_and_page` stays on the local state file in both modes; no paging
integration (PagerDuty, Slack) exists yet.
"""

import json
from collections import defaultdict

from agent.databricks_client import DatabricksClient, DatabricksError, load_config
from agent.llm import ToolResult

# DLT update states -> the RUNNING/SUCCESS/FAILED vocabulary the local
# state file and the DE prompt use. Anything not listed (INITIALIZING,
# SETTING_UP_TABLES, WAITING_FOR_RESOURCES, QUEUED, ...) is in progress.
_UPDATE_STATE_TO_STATUS = {"COMPLETED": "SUCCESS", "FAILED": "FAILED", "CANCELED": "CANCELED"}
_HEALTHY_STATUSES = ("RUNNING", "SUCCESS")


def live_backend() -> DatabricksClient | None:
    config = load_config()
    if config is None or not config.pipeline_ids:
        return None
    return DatabricksClient(config)


def _error(message: str) -> ToolResult:
    return ToolResult(tool_use_id="", content=json.dumps({"ok": False, "error": message}), is_error=True)


def _latest_update(pipeline: dict) -> dict:
    updates = pipeline.get("latest_updates") or []
    return updates[0] if updates else {}


def check_expectation_metrics(client: DatabricksClient) -> ToolResult:
    """Sums `failed_records` per (dataset, expectation) across the
    `flow_progress` events of each pipeline's latest update."""
    failing = []
    try:
        for name, pipeline_id in client.config.pipeline_ids.items():
            update_id = _latest_update(client.get_pipeline(pipeline_id)).get("update_id")
            failures: dict[tuple[str, str], int] = defaultdict(int)
            for event in client.list_pipeline_events(pipeline_id):
                if event.get("event_type") != "flow_progress":
                    continue
                if update_id and event.get("origin", {}).get("update_id") != update_id:
                    continue
                data_quality = event.get("details", {}).get("flow_progress", {}).get("data_quality", {})
                for expectation in data_quality.get("expectations") or []:
                    key = (expectation.get("dataset", ""), expectation.get("name", ""))
                    failures[key] += int(expectation.get("failed_records") or 0)
            failing.extend(
                {"pipeline": name, "table": dataset, "expectation": expectation, "failure_count": count}
                for (dataset, expectation), count in sorted(failures.items())
                if count > 0
            )
    except DatabricksError as exc:
        return _error(str(exc))
    return ToolResult(tool_use_id="", content=json.dumps({"ok": not failing, "failing_expectations": failing}))


def check_job_status(client: DatabricksClient) -> ToolResult:
    jobs = {}
    try:
        for name, pipeline_id in client.config.pipeline_ids.items():
            pipeline = client.get_pipeline(pipeline_id)
            latest = _latest_update(pipeline)
            if pipeline.get("state") == "FAILED":
                status = "FAILED"
            else:
                status = _UPDATE_STATE_TO_STATUS.get(latest.get("state", ""), "RUNNING")
            jobs[name] = {
                "status": status,
                "pipeline_id": pipeline_id,
                "latest_update_id": latest.get("update_id"),
            }
    except DatabricksError as exc:
        return _error(str(exc))
    unhealthy = {name: info["status"] for name, info in jobs.items() if info["status"] not in _HEALTHY_STATUSES}
    return ToolResult(
        tool_use_id="", content=json.dumps({"ok": not unhealthy, "jobs": jobs, "unhealthy": unhealthy})
    )


def restart_pipeline(client: DatabricksClient, pipeline_name: str) -> ToolResult:
    pipeline_id = client.config.pipeline_ids.get(pipeline_name)
    if pipeline_id is None:
        return _error(f"unknown pipeline: {pipeline_name}")
    try:
        update = client.start_update(pipeline_id)
    except DatabricksError as exc:
        return _error(str(exc))
    payload = {"ok": True, "pipeline": pipeline_name, "update_id": update.get("update_id"), "status": "STARTED"}
    return ToolResult(tool_use_id="", content=json.dumps(payload))


def unsupported(tool_name: str) -> ToolResult:
    return _error(
        f"{tool_name} has no live-workspace implementation (see agent/tools/pipeline_health_live.py); "
        "escalate to a human instead of retrying."
    )
