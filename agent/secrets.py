"""GCP Secret Manager integration — the SAME no-op-unless-configured pattern
`agent/llm.py`'s `Tracer` already uses for Langfuse: zero network calls and
zero `google.cloud.secretmanager` import unless `GCP_PROJECT_ID` is actually
set, so the whole test suite (and any local dev run without a GCP project)
never touches Google Cloud at all.

Intended real-deployment use: a value like `ANTHROPIC_API_KEY` or a Langfuse
key can be read via `get_secret("ANTHROPIC_API_KEY")` instead of
`os.environ["ANTHROPIC_API_KEY"]` directly, and it transparently comes from
Secret Manager when `GCP_PROJECT_ID` is configured (a real workspace) or
falls back to the plain environment variable otherwise (local dev, CI, this
sandbox) — callers don't need an if/else of their own.
"""

import os

import structlog

logger = structlog.get_logger(__name__)


def get_secret(name: str, *, project_id: str | None = None, version: str = "latest") -> str | None:
    """Returns the secret payload for `name`, or `None` if it can't be found
    anywhere.

    Resolution order:
    1. If `project_id` (or the `GCP_PROJECT_ID` env var) is set, try fetching
       `projects/{project}/secrets/{name}/versions/{version}` from GCP Secret
       Manager via `google-cloud-secret-manager`. The client library is
       imported lazily, inside this branch only, so it's never on the import
       path for a plain environment-variable-only deployment (mirrors
       `agent/llm.py:Tracer`'s lazy `from langfuse import Langfuse`).
    2. On ANY failure in step 1 (no `GCP_PROJECT_ID`, no real GCP
       credentials, network error, secret doesn't exist, client library not
       installed, etc.) — or simply because `GCP_PROJECT_ID` was never set —
       fall back to `os.environ.get(name)`. A Secret Manager outage or
       misconfiguration must never crash a caller that just wants a config
       value; it should behave exactly like an unset Secret Manager entry.
    """
    resolved_project_id = project_id or os.environ.get("GCP_PROJECT_ID")

    if resolved_project_id:
        try:
            secret_value = _fetch_from_secret_manager(resolved_project_id, name, version)
            if secret_value is not None:
                return secret_value
        except Exception:
            # Best-effort, exactly like agent/llm.py:Tracer._safe_trace —
            # observability/config plumbing must never break the caller's
            # real job. Falls through to the environment-variable fallback.
            logger.warning("secret_manager_lookup_failed_falling_back_to_env", secret_name=name)

    return os.environ.get(name)


def _fetch_from_secret_manager(project_id: str, name: str, version: str) -> str | None:
    # Imported lazily: only reached when GCP_PROJECT_ID is actually set, so
    # `google.cloud.secretmanager` is never imported (and no network call is
    # ever attempted) for the common local-dev/CI/test-suite case.
    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    secret_path = client.secret_version_path(project_id, name, version)
    response = client.access_secret_version(request={"name": secret_path})
    return response.payload.data.decode("utf-8")
