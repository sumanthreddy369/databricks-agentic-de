"""Operational metrics for Prometheus / Grafana, published through OpenTelemetry.

The goal metric of this project is "less human work, safely", so the
dashboard is built from the same records as the human-work report
(agent/human_work.py), not from separate counters that could drift from them:

- the append-only audit log and the approval history (requests, who needed
  a person, guardrail events, approvals and decision times, autonomy);
- the newest golden-set report (Genie vs Genie + guardrails vs metric path,
  latency);
- the anomaly-model evaluation (docs/eda/model_eval.json).

    uv run python -m agent.telemetry                 # export every 15 s until stopped
    uv run python -m agent.telemetry --once          # export once and exit

Metrics go over OTLP/HTTP to OTEL_EXPORTER_OTLP_ENDPOINT (default
http://localhost:4318: the OpenTelemetry Collector in docker-compose.yml,
profile "observability"), which Prometheus scrapes and Grafana charts. A
different backend (Datadog, Dynatrace, Grafana Cloud) is a collector config
change, not a code change. Labels carry modes, tools, outcomes and classes -
never patient data.
"""

import argparse
import json
import os
import socket
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent import autonomy, human_work
from agent.orchestrator import DEFAULT_AUDIT_LOG_PATH
from agent.otel import SERVICE_NAME

REPO_ROOT = Path(__file__).resolve().parent.parent
GOLDEN_DIRS = (REPO_ROOT / "docs" / "comparisons" / "results", REPO_ROOT / "evals" / "reports")
MODEL_EVAL_PATH = REPO_ROOT / "docs" / "eda" / "model_eval.json"

# name -> description. Every gauge is recomputed from the records at each export.
METRICS = {
    "agentic_de_requests": "Requests handled in the window, by mode (da = analyst, de = pipeline)",
    "agentic_de_requests_needing_person": "Requests that needed a person, by reason",
    "agentic_de_share_handled_without_person": "Share of requests in the window that needed no person (0-1)",
    "agentic_de_analyst_answers": "Analyst questions by the tool that answered them",
    "agentic_de_safety_events": "Guardrail events in the window, by type",
    "agentic_de_problems": "Incidents in the window, by problem-catalog id and title",
    "agentic_de_approvals": "Approval requests in the window, by outcome",
    "agentic_de_approval_decision_minutes": "Median minutes from approval request to a person's decision",
    "agentic_de_approvals_waiting": "Approval requests waiting for a person right now",
    "agentic_de_action_classes": "Remediation action classes by policy (approval / auto)",
    "agentic_de_action_class_streak": "Consecutive approved, successful runs per action class (10 = promotable)",
    "agentic_de_kill_switch_engaged": "1 if autonomous remediation is switched off",
    "agentic_de_golden_results": "Golden-set outcomes of the newest report, by configuration and result",
    "agentic_de_golden_run_age_hours": "Hours since the newest golden-set run",
    "agentic_de_answer_latency_seconds": "Golden-set answer latency, by path and statistic",
    "agentic_de_anomaly_eval_flag_rate": "Share of each labelled test set flagged, by suite and detector",
    "agentic_de_anomaly_detector_recall": "Share of should-flag panels each detector caught, by suite",
    "agentic_de_anomaly_detector_false_alarm_rate": "Share of stable panels each detector flagged, by suite",
    "agentic_de_anomaly_recall_at_matched_false_alarms": "Recall with the model's threshold matched to NEWS2's "
    "false-alarm rate on stable patients",
}


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            lines.append(json.loads(raw))
        except ValueError:
            continue
    return lines


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _catalog_titles() -> dict[str, str]:
    from agent.tools.problem_catalog import load_catalog

    return {p["id"]: p["title"] for p in load_catalog()}


def newest_golden_report(dirs=GOLDEN_DIRS) -> dict | None:
    """Newest run; for the same run, the re-scored report (current guardrails)."""
    best, best_key = None, None
    for d in dirs:
        for path in Path(d).glob("golden_*.json"):
            report = _read_json(path)
            if not isinstance(report, dict) or "run_at" not in report:
                continue
            key = (report["run_at"], bool(report.get("rescored_from")))
            if best_key is None or key > best_key:
                best, best_key = report, key
    return best


def snapshot(
    audit_lines: list[dict],
    state: dict,
    golden: dict | None,
    model_eval: dict | None,
    *,
    now: datetime,
    days: int = 7,
) -> list[tuple[str, dict, float]]:
    """[(metric name, labels, value)]. Pure: tested without any exporter."""
    out: list[tuple[str, dict, float]] = []
    approvals = state.get("pending_approvals", [])
    report = human_work.build(audit_lines, approvals, now - timedelta(days=days), now)

    for mode, n in report["requests"]["by_mode"].items():
        out.append(("agentic_de_requests", {"mode": mode}, n))
    needed = report["needed_a_person"]
    out.append(("agentic_de_requests_needing_person", {"reason": "escalation"}, needed["escalations"]))
    out.append(
        ("agentic_de_requests_needing_person", {"reason": "approval"}, needed["fixes_waiting_for_approval"])
    )
    if needed["share_handled_without_a_person"] is not None:
        out.append(("agentic_de_share_handled_without_person", {}, needed["share_handled_without_a_person"]))
    for source, n in report["analyst_answers_by_source"].items():
        out.append(("agentic_de_analyst_answers", {"source": source}, n))
    for event, n in report["safety_events"].items():
        out.append(("agentic_de_safety_events", {"event": event}, n))
    titles = _catalog_titles()
    for problem_id, n in report["problems_by_catalog_id"].items():
        out.append(("agentic_de_problems", {"id": problem_id, "title": titles.get(problem_id, "")}, n))
    for outcome, n in report["approvals"]["by_outcome"].items():
        out.append(("agentic_de_approvals", {"outcome": outcome}, n))
    if report["approvals"]["median_decision_minutes"] is not None:
        out.append(("agentic_de_approval_decision_minutes", {}, report["approvals"]["median_decision_minutes"]))
    out.append(("agentic_de_approvals_waiting", {}, sum(1 for a in approvals if a.get("status") == "pending")))

    classes = autonomy.summary(state)
    for policy in ("approval", "auto"):
        out.append(
            ("agentic_de_action_classes", {"policy": policy}, sum(1 for c in classes if c["policy"] == policy))
        )
    for c in classes:
        out.append(("agentic_de_action_class_streak", {"class": c["class"], "policy": c["policy"]}, c["streak"]))
    out.append(
        ("agentic_de_kill_switch_engaged", {}, 0 if state.get("autonomous_remediation_enabled", True) else 1)
    )

    if golden:
        for config, counts in golden.get("summary", {}).items():
            if config.endswith("_latency_s"):
                path = config.removesuffix("_latency_s")
                for stat, seconds in counts.items():
                    out.append(("agentic_de_answer_latency_seconds", {"path": path, "stat": stat}, seconds))
                continue
            for result, n in counts.items():
                if result != "questions":
                    out.append(("agentic_de_golden_results", {"config": config, "result": result}, n))
        run_at = datetime.strptime(golden["run_at"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        out.append(("agentic_de_golden_run_age_hours", {}, round((now - run_at).total_seconds() / 3600, 2)))

    for suite, body in (model_eval or {}).get("suites", {}).items():
        for test_set, row in body["sets"].items():
            for detector, rate in row["flag_rate"].items():
                labels = {"suite": suite, "test_set": test_set, "detector": detector}
                out.append(("agentic_de_anomaly_eval_flag_rate", labels, rate))
        for detector, m in body["summary"].items():
            labels = {"suite": suite, "detector": detector}
            out.append(("agentic_de_anomaly_detector_recall", labels, m["recall"]))
            out.append(("agentic_de_anomaly_detector_false_alarm_rate", labels, m["false_positive_rate"]))
    matched = (model_eval or {}).get("decision", {}).get("recall_at_matched_false_alarms", {})
    for detector, recall in matched.items():
        out.append(("agentic_de_anomaly_recall_at_matched_false_alarms", {"detector": detector}, recall))
    return out


class _Source:
    """Reads the records once per export cycle, shared by all gauge callbacks."""

    def __init__(self, audit_log_path: Path, state_path: Path, days: int):
        self.audit_log_path, self.state_path, self.days = audit_log_path, state_path, days
        self._lock = threading.Lock()
        self._at = 0.0
        self._rows: list[tuple[str, dict, float]] = []

    def rows(self, name: str) -> list[tuple[dict, float]]:
        with self._lock:
            if time.monotonic() - self._at > 1.0:
                self._rows = snapshot(
                    _read_jsonl(self.audit_log_path),
                    _read_json(self.state_path) or {},
                    newest_golden_report(),
                    _read_json(MODEL_EVAL_PATH),
                    now=datetime.now(UTC),
                    days=self.days,
                )
                self._at = time.monotonic()
        return [(labels, value) for metric, labels, value in self._rows if metric == name]


def build_meter_provider(source: _Source, reader):
    from opentelemetry.metrics import Observation
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.resources import Resource

    # A stable instance id: the SDK's default is random per process, so every
    # restart would add a duplicate copy of each series until the old expired.
    resource = Resource.create({"service.name": SERVICE_NAME, "service.instance.id": socket.gethostname()})
    provider = MeterProvider(resource=resource, metric_readers=[reader])
    meter = provider.get_meter(SERVICE_NAME)
    for name, description in METRICS.items():

        def callback(_options, name=name):
            return [Observation(value, labels) for labels, value in source.rows(name)]

        meter.create_observable_gauge(name, callbacks=[callback], description=description)
    return provider


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--audit-log-path", default=os.environ.get("AUDIT_LOG_PATH", DEFAULT_AUDIT_LOG_PATH))
    parser.add_argument(
        "--state-path", default=os.environ.get("PIPELINE_STATE_PATH", human_work.DEFAULT_STATE_PATH)
    )
    parser.add_argument("--days", type=int, default=7, help="window for request/approval counts")
    parser.add_argument("--interval", type=float, default=15.0, help="seconds between exports")
    parser.add_argument("--once", action="store_true", help="export once and exit")
    args = parser.parse_args(argv)

    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

    os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    source = _Source(Path(args.audit_log_path), Path(args.state_path), args.days)
    reader = PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=int(args.interval * 1000))
    provider = build_meter_provider(source, reader)
    endpoint = os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"]
    print(f"exporting {len(METRICS)} metrics to {endpoint} every {args.interval:g}s")
    print(f"reading {args.audit_log_path} and {args.state_path}")
    try:
        if args.once:
            provider.force_flush()
        else:
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        provider.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
