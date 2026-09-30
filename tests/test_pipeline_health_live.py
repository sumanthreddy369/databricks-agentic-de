"""Proves agent/tools/pipeline_health.py's delegation to the live-workspace
backend (agent/tools/pipeline_health_live.py) and that backend's parsing of
Pipelines API responses, against a mocked REST transport — zero network
calls. The response fixtures follow the documented Pipelines API shapes;
they are not captured from a real workspace (none exists here).
"""

import json

import httpx
import pytest

from agent.databricks_client import DatabricksClient
from agent.tools import pipeline_health, pipeline_health_live

PIPELINE_IDS = {"streaming_patient_pipeline": "pl-stream", "reference_data_pipeline": "pl-ref"}


def _flow_progress(update_id, dataset, name, failed):
    return {
        "event_type": "flow_progress",
        "origin": {"update_id": update_id},
        "details": {
            "flow_progress": {
                "data_quality": {
                    "expectations": [
                        {"dataset": dataset, "name": name, "passed_records": 100, "failed_records": failed}
                    ]
                }
            }
        },
    }


class FakeWorkspace:
    """Serves canned Pipelines API responses and records every request."""

    def __init__(self):
        self.requests: list[tuple[str, str, dict | None]] = []
        self.pipelines = {
            "pl-stream": {"state": "RUNNING", "latest_updates": [{"update_id": "upd-2", "state": "RUNNING"}]},
            "pl-ref": {"state": "IDLE", "latest_updates": [{"update_id": "upd-r", "state": "COMPLETED"}]},
        }
        self.events = {
            "pl-stream": [
                _flow_progress("upd-2", "silver_fct_vitals", "plausible_vital_value", 4),
                _flow_progress("upd-2", "silver_fct_vitals", "plausible_vital_value", 3),
                _flow_progress("upd-2", "silver_fct_vitals", "known_vital_item", 0),
                # An older update's failures must not be counted.
                _flow_progress("upd-1", "silver_fct_vitals", "known_vital_item", 50),
                {"event_type": "update_progress", "origin": {"update_id": "upd-2"}},
            ],
            "pl-ref": [],
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, body))
        parts = request.url.path.split("/")  # ['', 'api', '2.0', 'pipelines', id, ...]
        pipeline_id = parts[4]
        if request.method == "GET" and len(parts) == 5:
            return httpx.Response(200, json=self.pipelines[pipeline_id])
        if request.method == "GET" and parts[5] == "events":
            return httpx.Response(200, json={"events": self.events[pipeline_id]})
        if request.method == "POST" and parts[5] == "updates":
            return httpx.Response(200, json={"update_id": "upd-new"})
        return httpx.Response(404)


@pytest.fixture
def workspace(monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", "https://example.gcp.databricks.com")
    monkeypatch.setenv("DATABRICKS_TOKEN", "test-token")
    monkeypatch.setenv("DATABRICKS_PIPELINE_IDS", json.dumps(PIPELINE_IDS))
    monkeypatch.delenv("GCP_PROJECT_ID", raising=False)
    fake = FakeWorkspace()
    monkeypatch.setattr(
        pipeline_health_live,
        "DatabricksClient",
        lambda config: DatabricksClient(config, transport=httpx.MockTransport(fake.handler)),
    )
    return fake


@pytest.fixture
def state_path(tmp_path):
    # Live mode never reads this for the delegated tools; a nonexistent path
    # proves it (the local implementations would raise FileNotFoundError).
    return tmp_path / "does_not_exist.json"


def test_no_live_backend_without_pipeline_ids(monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", "https://example.gcp.databricks.com")
    monkeypatch.setenv("DATABRICKS_TOKEN", "test-token")
    monkeypatch.delenv("GCP_PROJECT_ID", raising=False)

    assert pipeline_health_live.live_backend() is None


def test_expectation_metrics_sum_failures_for_the_latest_update_only(workspace, state_path):
    result = pipeline_health.check_expectation_metrics(state_path)

    payload = json.loads(result.content)
    assert result.is_error is False
    assert payload["ok"] is False
    assert payload["failing_expectations"] == [
        {
            "pipeline": "streaming_patient_pipeline",
            "table": "silver_fct_vitals",
            "expectation": "plausible_vital_value",
            "failure_count": 7,
        }
    ]


def test_job_status_maps_update_states(workspace, state_path):
    workspace.pipelines["pl-stream"] = {
        "state": "FAILED",
        "latest_updates": [{"update_id": "u", "state": "FAILED"}],
    }

    payload = json.loads(pipeline_health.check_job_status(state_path).content)

    assert payload["jobs"]["streaming_patient_pipeline"]["status"] == "FAILED"
    assert payload["jobs"]["reference_data_pipeline"]["status"] == "SUCCESS"
    assert payload["unhealthy"] == {"streaming_patient_pipeline": "FAILED"}
    assert payload["ok"] is False


def test_healthy_pipelines_report_ok(workspace, state_path):
    payload = json.loads(pipeline_health.check_job_status(state_path).content)

    assert payload["ok"] is True
    assert payload["jobs"]["streaming_patient_pipeline"]["status"] == "RUNNING"


def test_restart_starts_an_incremental_update(workspace, state_path):
    result = pipeline_health.restart_pipeline(state_path, "streaming_patient_pipeline")

    assert json.loads(result.content) == {
        "ok": True,
        "pipeline": "streaming_patient_pipeline",
        "update_id": "upd-new",
        "status": "STARTED",
    }
    assert ("POST", "/api/2.0/pipelines/pl-stream/updates", {"full_refresh": False}) in workspace.requests


def test_restart_of_unknown_pipeline_makes_no_request(workspace, state_path):
    result = pipeline_health.restart_pipeline(state_path, "not_a_pipeline")

    assert result.is_error is True
    assert workspace.requests == []


@pytest.mark.parametrize(
    "call",
    [
        lambda path: pipeline_health.detect_schema_drift(path),
        lambda path: pipeline_health.quarantine_bad_records(path, "silver_fct_vitals", "plausible_vital_value"),
    ],
)
def test_tools_without_a_live_equivalent_refuse_and_say_escalate(workspace, state_path, call):
    result = call(state_path)

    assert result.is_error is True
    assert "escalate" in json.loads(result.content)["error"]
    assert workspace.requests == []


def test_api_errors_become_tool_errors(workspace, state_path, monkeypatch):
    monkeypatch.setattr(workspace, "handler", lambda request: httpx.Response(403, text="PERMISSION_DENIED"))
    monkeypatch.setattr(
        pipeline_health_live,
        "DatabricksClient",
        lambda config: DatabricksClient(config, transport=httpx.MockTransport(workspace.handler)),
    )

    result = pipeline_health.check_job_status(state_path)

    assert result.is_error is True
    assert "HTTP 403" in json.loads(result.content)["error"]
