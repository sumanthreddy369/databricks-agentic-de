"""Proves agent/tools/knowledge_search.py's graceful-degradation path: without
a configured Databricks workspace, `search_clinical_knowledge` returns a
clear "not configured" result and makes zero network calls — it never
crashes. This is a Tier 2 scaffold (see that module's own docstring); this
test proves the degradation path only, not a real Vector Search query
(no live workspace exists in this environment to query).
"""

import json
import sys
from unittest.mock import MagicMock

from agent.tools.knowledge_search import search_clinical_knowledge


def test_returns_not_configured_when_no_databricks_env_vars_are_set(monkeypatch):
    monkeypatch.delenv("DATABRICKS_HOST", raising=False)
    monkeypatch.delenv("DATABRICKS_TOKEN", raising=False)
    monkeypatch.delenv("VECTOR_SEARCH_ENDPOINT", raising=False)

    result = search_clinical_knowledge("what is the sepsis escalation protocol?")

    assert result.is_error is False
    payload = json.loads(result.content)
    assert payload["ok"] is False
    assert payload["configured"] is False


def test_returns_not_configured_when_endpoint_missing_even_with_host_and_token(monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", "https://example.gcp.databricks.com")
    monkeypatch.setenv("DATABRICKS_TOKEN", "fake-token")
    monkeypatch.delenv("VECTOR_SEARCH_ENDPOINT", raising=False)

    result = search_clinical_knowledge("fall risk protocol")

    payload = json.loads(result.content)
    assert payload["ok"] is False
    assert payload["configured"] is False


def test_never_imports_vectorsearch_client_when_not_configured(monkeypatch):
    monkeypatch.delenv("DATABRICKS_HOST", raising=False)
    monkeypatch.delenv("DATABRICKS_TOKEN", raising=False)
    monkeypatch.delenv("VECTOR_SEARCH_ENDPOINT", raising=False)
    # If the lazy `from databricks.vector_search.client import
    # VectorSearchClient` were ever reached despite being unconfigured, this
    # poisoned module would raise the moment it's imported.
    monkeypatch.setitem(sys.modules, "databricks.vector_search.client", None)

    result = search_clinical_knowledge("isolation precautions")

    assert json.loads(result.content)["ok"] is False


def test_returns_not_configured_when_client_library_is_not_installed(monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", "https://example.gcp.databricks.com")
    monkeypatch.setenv("DATABRICKS_TOKEN", "fake-token")
    monkeypatch.setenv("VECTOR_SEARCH_ENDPOINT", "clinical-knowledge-endpoint")
    monkeypatch.setitem(sys.modules, "databricks.vector_search.client", None)

    result = search_clinical_knowledge("medication reconciliation")

    assert result.is_error is False
    payload = json.loads(result.content)
    assert payload["ok"] is False
    assert payload["configured"] is False


def test_uses_mocked_vector_search_client_when_fully_configured(monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", "https://example.gcp.databricks.com")
    monkeypatch.setenv("DATABRICKS_TOKEN", "fake-token")
    monkeypatch.setenv("VECTOR_SEARCH_ENDPOINT", "clinical-knowledge-endpoint")

    fake_index = MagicMock()
    fake_index.similarity_search.return_value = {"result": {"data_array": [["sepsis alert protocol text"]]}}
    fake_client = MagicMock()
    fake_client.get_index.return_value = fake_index
    fake_client_module = MagicMock()
    fake_client_module.VectorSearchClient.return_value = fake_client
    monkeypatch.setitem(sys.modules, "databricks.vector_search.client", fake_client_module)

    result = search_clinical_knowledge("sepsis escalation")

    assert result.is_error is False
    payload = json.loads(result.content)
    assert payload["ok"] is True
    fake_client.get_index.assert_called_once_with(
        endpoint_name="clinical-knowledge-endpoint",
        index_name="healthcare_agentic_de.gold.clinical_protocols_index",
    )


def test_real_vector_search_failure_is_reported_as_an_error_not_a_crash(monkeypatch):
    monkeypatch.setenv("DATABRICKS_HOST", "https://example.gcp.databricks.com")
    monkeypatch.setenv("DATABRICKS_TOKEN", "fake-token")
    monkeypatch.setenv("VECTOR_SEARCH_ENDPOINT", "clinical-knowledge-endpoint")

    fake_client = MagicMock()
    fake_client.get_index.side_effect = RuntimeError("index not found")
    fake_client_module = MagicMock()
    fake_client_module.VectorSearchClient.return_value = fake_client
    monkeypatch.setitem(sys.modules, "databricks.vector_search.client", fake_client_module)

    result = search_clinical_knowledge("fall risk protocol")

    assert result.is_error is True
    payload = json.loads(result.content)
    assert payload["ok"] is False
    assert "index not found" in payload["error"]
