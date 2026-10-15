"""Sends escalations to a person - once per problem, not once per check.

notify_and_page (agent/tools/pipeline_health.py) calls this. Two parts:

- Channel: ALERT_WEBHOOK_URL, if set, gets a POST with {"text": message}
  (the shape Slack incoming webhooks and many chat tools accept). Without
  it nothing is sent here; the incident is still recorded and the scheduled
  job's failure email remains the channel. A failed send is reported in the
  result, never raised - alerting must not break the check that needed it.
- De-duplication: a health check runs every 15 minutes; an unresolved
  problem would otherwise alert every run. Each escalation has a
  fingerprint (which problems, not their counts); the same fingerprint
  alerts again only after ALERT_REPEAT_HOURS (default 24).

The webhook URL is a secret: it comes from the environment (on Databricks,
a secret scope the account owner sets) and is never logged or returned.
"""

import hashlib
import os
from datetime import UTC, datetime, timedelta

import httpx

DEFAULT_REPEAT_HOURS = 24.0


def fingerprint_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def send_alert(
    state: dict,
    message: str,
    *,
    fingerprint: str | None = None,
    now: datetime | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict:
    """Mutates state["alerts"] (fingerprint -> last time alerted); returns what happened."""
    now = now or datetime.now(UTC)
    fingerprint = fingerprint or fingerprint_of(message)
    repeat_hours = float(os.environ.get("ALERT_REPEAT_HOURS", DEFAULT_REPEAT_HOURS))
    alerts = state.setdefault("alerts", {})

    last = alerts.get(fingerprint)
    if last and now - datetime.fromisoformat(last) < timedelta(hours=repeat_hours):
        return {"fingerprint": fingerprint, "new": False, "sent": False, "reason": f"already alerted at {last}"}

    url = os.environ.get("ALERT_WEBHOOK_URL")
    if not url:
        alerts[fingerprint] = now.isoformat()
        return {"fingerprint": fingerprint, "new": True, "sent": False, "channel": "none configured"}
    try:
        with httpx.Client(timeout=10.0, transport=transport) as client:
            response = client.post(url, json={"text": message})
        response.raise_for_status()
    except httpx.HTTPError as exc:
        # Not marked as alerted: the next check tries again.
        return {"fingerprint": fingerprint, "new": True, "sent": False, "error": type(exc).__name__}
    alerts[fingerprint] = now.isoformat()
    return {"fingerprint": fingerprint, "new": True, "sent": True, "channel": "webhook"}
