"""Proves agent/secrets.py's no-op-unless-configured pattern: without
GCP_PROJECT_ID set, get_secret() never imports google.cloud.secretmanager and
falls back straight to the environment; with a project id AND a mocked
Secret Manager client, it fetches through that client instead. Zero real GCP
calls either way.
"""

import sys
from unittest.mock import MagicMock

from agent.secrets import get_secret


def test_falls_back_to_environment_when_no_project_id_configured(monkeypatch):
    monkeypatch.delenv("GCP_PROJECT_ID", raising=False)
    monkeypatch.setenv("SOME_TEST_SECRET", "plain-env-value")

    assert get_secret("SOME_TEST_SECRET") == "plain-env-value"


def test_returns_none_when_neither_project_id_nor_env_var_are_set(monkeypatch):
    monkeypatch.delenv("GCP_PROJECT_ID", raising=False)
    monkeypatch.delenv("SOME_UNSET_TEST_SECRET", raising=False)

    assert get_secret("SOME_UNSET_TEST_SECRET") is None


def test_never_imports_secret_manager_client_library_without_project_id(monkeypatch):
    monkeypatch.delenv("GCP_PROJECT_ID", raising=False)
    monkeypatch.setenv("SOME_TEST_SECRET", "plain-env-value")
    # Guarantees the lazy `from google.cloud import secretmanager` inside
    # _fetch_from_secret_manager is never reached: if it were, this poisoned
    # module would raise the moment it's imported.
    monkeypatch.setitem(sys.modules, "google.cloud.secretmanager", None)

    assert get_secret("SOME_TEST_SECRET") == "plain-env-value"


def test_uses_mocked_secret_manager_client_when_project_id_is_configured(monkeypatch):
    fake_client = MagicMock()
    fake_client.secret_version_path.return_value = "projects/p/secrets/MY_SECRET/versions/latest"
    fake_response = MagicMock()
    fake_response.payload.data = b"secret-from-gcp"
    fake_client.access_secret_version.return_value = fake_response

    fake_secretmanager_module = MagicMock()
    fake_secretmanager_module.SecretManagerServiceClient.return_value = fake_client
    monkeypatch.setitem(sys.modules, "google.cloud.secretmanager", fake_secretmanager_module)

    result = get_secret("MY_SECRET", project_id="p")

    assert result == "secret-from-gcp"
    fake_client.secret_version_path.assert_called_once_with("p", "MY_SECRET", "latest")
    fake_client.access_secret_version.assert_called_once()


def test_falls_back_to_environment_when_secret_manager_client_raises(monkeypatch):
    monkeypatch.setenv("SOME_TEST_SECRET", "fallback-value")

    fake_secretmanager_module = MagicMock()
    fake_secretmanager_module.SecretManagerServiceClient.side_effect = RuntimeError("no real GCP credentials here")
    monkeypatch.setitem(sys.modules, "google.cloud.secretmanager", fake_secretmanager_module)

    result = get_secret("SOME_TEST_SECRET", project_id="p")

    assert result == "fallback-value"


def test_falls_back_to_environment_when_secret_does_not_exist_in_secret_manager(monkeypatch):
    monkeypatch.setenv("SOME_TEST_SECRET", "fallback-value")

    fake_client = MagicMock()
    fake_client.access_secret_version.side_effect = Exception("NotFound: secret does not exist")
    fake_secretmanager_module = MagicMock()
    fake_secretmanager_module.SecretManagerServiceClient.return_value = fake_client
    monkeypatch.setitem(sys.modules, "google.cloud.secretmanager", fake_secretmanager_module)

    assert get_secret("SOME_TEST_SECRET", project_id="p") == "fallback-value"
