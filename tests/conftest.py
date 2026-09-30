"""Suite-wide guarantee that no test reaches a real Databricks workspace.

agent/databricks_client.py switches DA- and DE-mode tools to live REST calls
whenever DATABRICKS_HOST/DATABRICKS_TOKEN are set. A developer with those in
their shell (or a .env loaded elsewhere) must still get a zero-network test
run, so they're cleared before every test. Tests that exercise the live path
set them explicitly and inject a mocked httpx transport.
"""

import pytest

_LIVE_WORKSPACE_ENV_VARS = (
    "DATABRICKS_HOST",
    "DATABRICKS_TOKEN",
    "DATABRICKS_WAREHOUSE_ID",
    "DATABRICKS_PIPELINE_IDS",
)


@pytest.fixture(autouse=True)
def _no_live_databricks_workspace(monkeypatch):
    for name in _LIVE_WORKSPACE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
