"""Proves agent/databricks_client.py's config gating and request/response
handling against a mocked httpx transport — zero network calls. This proves
the client sends the documented request shapes and parses the documented
response shapes; it does not prove a real workspace accepts them (none
exists in this environment).
"""

import json

import httpx
import pytest

from agent.databricks_client import DatabricksClient, DatabricksConfig, DatabricksError, load_config

HOST = "https://example.gcp.databricks.com"


def _client(handler, **config_overrides) -> DatabricksClient:
    config = DatabricksConfig(host=HOST, token="test-token", warehouse_id="wh-123", **config_overrides)
    return DatabricksClient(config, transport=httpx.MockTransport(handler))


def _succeeded(columns, data_array):
    return {
        "status": {"state": "SUCCEEDED"},
        "manifest": {"schema": {"columns": columns}},
        "result": {"data_array": data_array},
    }


def test_load_config_is_none_without_host(monkeypatch):
    monkeypatch.setenv("DATABRICKS_TOKEN", "test-token")
    assert load_config() is None


def test_load_config_is_none_without_token(monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", HOST)
    monkeypatch.delenv("GCP_PROJECT_ID", raising=False)
    assert load_config() is None


def test_load_config_reads_all_settings(monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", HOST + "/")
    monkeypatch.setenv("DATABRICKS_TOKEN", "test-token")
    monkeypatch.delenv("GCP_PROJECT_ID", raising=False)
    monkeypatch.setenv("DATABRICKS_WAREHOUSE_ID", "wh-123")
    monkeypatch.setenv("DATABRICKS_PIPELINE_IDS", json.dumps({"streaming_patient_pipeline": "pl-1"}))

    config = load_config()

    assert config.host == HOST
    assert config.warehouse_id == "wh-123"
    assert config.pipeline_ids == {"streaming_patient_pipeline": "pl-1"}
    assert "test-token" not in repr(config)


@pytest.mark.parametrize("raw", ["not json", '["pl-1"]', '{"streaming_patient_pipeline": 7}'])
def test_load_config_ignores_malformed_pipeline_ids(monkeypatch, raw):
    monkeypatch.setenv("DATABRICKS_HOST", HOST)
    monkeypatch.setenv("DATABRICKS_TOKEN", "test-token")
    monkeypatch.delenv("GCP_PROJECT_ID", raising=False)
    monkeypatch.setenv("DATABRICKS_PIPELINE_IDS", raw)

    assert load_config().pipeline_ids == {}


def test_execute_statement_sends_bound_parameters_and_converts_types():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = json.loads(request.content)
        columns = [
            {"name": "unit", "type_name": "STRING"},
            {"name": "reading_count", "type_name": "LONG"},
            {"name": "avg_heart_rate", "type_name": "DOUBLE"},
            {"name": "is_icu", "type_name": "BOOLEAN"},
        ]
        return httpx.Response(
            200, json=_succeeded(columns, [["ICU", "12", "91.5", "true"], ["ED", None, None, None]])
        )

    rows = _client(handler).execute_statement(
        "SELECT * FROM t WHERE unit = :p0", [{"name": "p0", "value": "ICU", "type": "STRING"}]
    )

    assert seen["url"] == f"{HOST}/api/2.0/sql/statements/"
    assert seen["auth"] == "Bearer test-token"
    assert seen["body"]["warehouse_id"] == "wh-123"
    assert seen["body"]["statement"] == "SELECT * FROM t WHERE unit = :p0"
    assert seen["body"]["parameters"] == [{"name": "p0", "value": "ICU", "type": "STRING"}]
    assert seen["body"]["on_wait_timeout"] == "CANCEL"
    assert rows == [
        {"unit": "ICU", "reading_count": 12, "avg_heart_rate": 91.5, "is_icu": True},
        {"unit": "ED", "reading_count": None, "avg_heart_rate": None, "is_icu": None},
    ]


def test_execute_statement_raises_on_failed_statement():
    def handler(request):
        return httpx.Response(
            200, json={"status": {"state": "FAILED", "error": {"message": "TABLE_OR_VIEW_NOT_FOUND"}}}
        )

    with pytest.raises(DatabricksError, match="TABLE_OR_VIEW_NOT_FOUND"):
        _client(handler).execute_statement("SELECT 1")


def test_http_errors_raise_databricks_error():
    def handler(request):
        return httpx.Response(403, text="PERMISSION_DENIED")

    with pytest.raises(DatabricksError, match="HTTP 403"):
        _client(handler).get_pipeline("pl-1")


def test_execute_statement_requires_a_warehouse():
    client = DatabricksClient(
        DatabricksConfig(host=HOST, token="t"), transport=httpx.MockTransport(lambda r: None)
    )
    with pytest.raises(DatabricksError, match="DATABRICKS_WAREHOUSE_ID"):
        client.execute_statement("SELECT 1")


def test_start_update_is_never_a_full_refresh():
    seen = {}

    def handler(request):
        seen["method"], seen["path"] = request.method, request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"update_id": "upd-9"})

    assert _client(handler).start_update("pl-1") == {"update_id": "upd-9"}
    assert (seen["method"], seen["path"]) == ("POST", "/api/2.0/pipelines/pl-1/updates")
    assert seen["body"] == {"full_refresh": False}
