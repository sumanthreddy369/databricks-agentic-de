"""Writes the Grafana dashboard (observability/grafana/dashboards/agentic_de.json).

    uv run python scripts/build_grafana_dashboard.py

Every panel queries a metric defined in agent/telemetry.py:METRICS;
tests/test_telemetry.py fails if the JSON is stale or a panel queries a
metric that doesn't exist. Colors follow the dataviz reference palette:
one categorical hue for bars, status colors only for good/bad states.
"""

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

OUT = REPO / "observability" / "grafana" / "dashboards" / "agentic_de.json"
BLUE, GOOD, WARNING, CRITICAL = "#2a78d6", "#0ca30c", "#fab219", "#d03b3b"
DS = {"type": "prometheus", "uid": "prometheus"}


def _target(expr: str, legend: str = "") -> dict:
    return {"datasource": DS, "expr": expr, "instant": True, "legendFormat": legend, "refId": "A"}


def _dedup(expr: str, legend: str = "") -> str:
    """Collapse duplicate series (e.g. an old exporter instance still within
    the collector's expiry) so a restart never doubles a panel."""
    labels = re.findall(r"\{\{(\w+)\}\}", legend)
    return f"max by ({', '.join(labels)}) ({expr})" if labels else f"max({expr})"


def stat(title, expr, pos, *, unit="none", decimals=None, thresholds=None, description="", mappings=None):
    defaults = {
        "unit": unit,
        "color": {"mode": "thresholds"},
        "thresholds": {"mode": "absolute", "steps": thresholds or [{"color": BLUE, "value": None}]},
    }
    if decimals is not None:
        defaults["decimals"] = decimals
    if mappings:
        defaults["mappings"] = mappings
    return {
        "type": "stat",
        "title": title,
        "description": description,
        "datasource": DS,
        "gridPos": pos,
        "targets": [_target(expr if expr.startswith("sum") else _dedup(expr))],
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "colorMode": "value",
            "graphMode": "none",
            "textMode": "value",
        },
    }


def bars(title, expr, legend, pos, *, unit="none", max_value=None, description=""):
    defaults = {"unit": unit, "min": 0, "color": {"mode": "fixed", "fixedColor": BLUE}}
    if max_value is not None:
        defaults["max"] = max_value
    return {
        "type": "bargauge",
        "title": title,
        "description": description,
        "datasource": DS,
        "gridPos": pos,
        "targets": [_target(_dedup(expr, legend), legend)],
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {
            "orientation": "horizontal",
            "displayMode": "basic",
            "showUnfilled": True,
            "namePlacement": "left",
            "text": {"titleSize": 13, "valueSize": 15},
            "minVizHeight": 14,
            "maxVizHeight": 22,
            "valueMode": "text",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        },
    }


def row(title, y):
    return {"type": "row", "title": title, "collapsed": False, "gridPos": {"h": 1, "w": 24, "x": 0, "y": y}}


def panels() -> list[dict]:
    p = []
    p.append(row("Human work - last 7 days, from the audit log and approval history", 0))
    p.append(
        stat(
            "Handled without a person",
            "agentic_de_share_handled_without_person",
            {"h": 5, "w": 6, "x": 0, "y": 1},
            unit="percentunit",
            decimals=0,
            description="Requests that needed no escalation and no approval. Upper bound: it only sees work "
            "that went through this layer.",
        )
    )
    p.append(stat("Requests", "sum(max by (mode) (agentic_de_requests))", {"h": 5, "w": 4, "x": 6, "y": 1}))
    p.append(
        stat(
            "Approvals waiting",
            "agentic_de_approvals_waiting",
            {"h": 5, "w": 4, "x": 10, "y": 1},
            thresholds=[{"color": GOOD, "value": None}, {"color": WARNING, "value": 1}],
        )
    )
    p.append(
        stat(
            "Median decision time",
            "agentic_de_approval_decision_minutes",
            {"h": 5, "w": 5, "x": 14, "y": 1},
            unit="m",
            decimals=1,
            description="Minutes from an approval request to a person's decision.",
        )
    )
    p.append(
        stat(
            "Autonomous remediation",
            "agentic_de_kill_switch_engaged",
            {"h": 5, "w": 5, "x": 19, "y": 1},
            thresholds=[{"color": GOOD, "value": None}, {"color": CRITICAL, "value": 1}],
            mappings=[
                {
                    "type": "value",
                    "options": {"0": {"text": "Enabled"}, "1": {"text": "Kill switch ON"}},
                }
            ],
        )
    )
    p.append(bars("Requests by mode", "agentic_de_requests", "{{mode}}", {"h": 7, "w": 6, "x": 0, "y": 6}))
    p.append(
        bars(
            "Needed a person, by reason",
            "agentic_de_requests_needing_person",
            "{{reason}}",
            {"h": 7, "w": 6, "x": 6, "y": 6},
        )
    )
    p.append(
        bars(
            "Analyst answers, by source",
            "agentic_de_analyst_answers",
            "{{source}}",
            {"h": 7, "w": 6, "x": 12, "y": 6},
        )
    )
    p.append(
        bars("Approvals, by outcome", "agentic_de_approvals", "{{outcome}}", {"h": 7, "w": 6, "x": 18, "y": 6})
    )

    p.append(row("Guardrails and earned autonomy", 13))
    p.append(
        bars(
            "Guardrail events, by type",
            "agentic_de_safety_events",
            "{{event}}",
            {"h": 8, "w": 12, "x": 0, "y": 14},
            description="Identifier requests refused, results withheld, small groups suppressed, untrusted text "
            "wrapped.",
        )
    )
    p.append(
        bars(
            "Fix classes: consecutive approved runs (10 = can be promoted)",
            "agentic_de_action_class_streak",
            "{{class}} ({{policy}})",
            {"h": 8, "w": 12, "x": 12, "y": 14},
            max_value=10,
        )
    )
    p.append(
        bars(
            "Incidents, by problem-catalog entry",
            "agentic_de_problems",
            "{{id}} {{title}}",
            {"h": 7, "w": 24, "x": 0, "y": 44},
        )
    )

    p.append(row("Measured quality - golden question set and anomaly-model evaluation", 22))
    p.append(
        bars(
            "Golden set: outcomes by configuration",
            'agentic_de_golden_results{result=~"correct|pass"}',
            "{{config}} {{result}}",
            {"h": 9, "w": 8, "x": 0, "y": 23},
            description="Newest golden report (re-scored with the current guardrails when available).",
        )
    )
    p.append(
        bars(
            "Golden set: fails + non-answers, by configuration",
            # Every configuration, zeros included - a lone bar can't show the comparison.
            'sum by (config) (agentic_de_golden_results{result!~"correct|pass"})'
            " or 0 * sum by (config) (agentic_de_golden_results)",
            "{{config}}",
            {"h": 9, "w": 8, "x": 8, "y": 23},
        )
    )
    p.append(
        bars(
            "Answer latency",
            'agentic_de_answer_latency_seconds{stat=~"p50|p90"}',
            "{{path}} {{stat}}",
            {"h": 9, "w": 8, "x": 16, "y": 23},
            unit="s",
        )
    )
    p.append(
        bars(
            "Deterioration caught (simulated patients)",
            'agentic_de_anomaly_detector_recall{suite="physiology"}',
            "{{detector}}",
            {"h": 6, "w": 8, "x": 0, "y": 32},
            unit="percentunit",
            max_value=1,
            description="Share of deteriorating panels (4 episode types, early and established) each detector "
            "flagged. From docs/eda/model_eval.json.",
        )
    )
    p.append(
        bars(
            "False alarms on stable patients",
            'agentic_de_anomaly_detector_false_alarm_rate{suite="physiology"}',
            "{{detector}}",
            {"h": 6, "w": 8, "x": 8, "y": 32},
            unit="percentunit",
            max_value=0.1,
        )
    )
    p.append(
        bars(
            "Caught at the same false-alarm rate as NEWS2",
            "agentic_de_anomaly_recall_at_matched_false_alarms",
            "{{detector}}",
            {"h": 6, "w": 16, "x": 0, "y": 38},
            unit="percentunit",
            max_value=1,
            description="Model threshold set to raise as many false alarms as NEWS2; the fair comparison "
            "behind promoting it to a calibrated extra signal.",
        )
    )
    p.append(
        stat(
            "Golden run age",
            "agentic_de_golden_run_age_hours",
            {"h": 5, "w": 8, "x": 16, "y": 32},
            unit="h",
            decimals=0,
            thresholds=[{"color": GOOD, "value": None}, {"color": WARNING, "value": 168}],
            description="Hours since the newest golden-set run. Amber after a week.",
        )
    )
    p.append(
        {
            "type": "text",
            "title": "Traces",
            "gridPos": {"h": 7, "w": 8, "x": 16, "y": 37},
            "options": {
                "mode": "markdown",
                "content": "Every request is a trace (`agent.request`) with one span per tool call "
                "(`agent.tool <name>`, attribute `tool.outcome`).\n\nOpen **Explore → Jaeger**, service "
                "`databricks-agentic-de`, or Jaeger at http://localhost:16686.",
            },
        }
    )
    p.append(row("Pipeline on Databricks - newest workspace snapshot (scripts/workspace_snapshot.py)", 51))
    p.append(
        stat(
            "Names / MRNs masked in Gold",
            "agentic_de_phi_masked_share",
            {"h": 5, "w": 6, "x": 0, "y": 52},
            unit="percentunit",
            decimals=0,
            thresholds=[{"color": CRITICAL, "value": None}, {"color": GOOD, "value": 1}],
            description="Share of Gold patients whose name and MRN Unity Catalog returns redacted. Anything below "
            "100% is a governance failure.",
        )
    )
    p.append(
        bars(
            "Identity problems flagged (M2)",
            'agentic_de_identity_conflicts_flagged{kind!=""}',
            "{{kind}}",
            {"h": 5, "w": 6, "x": 6, "y": 52},
            description="Two people on one patient_id, and one person under several. Flagged, never merged: a "
            "data steward decides.",
        )
    )
    p.append(
        stat(
            "Snapshot age",
            "agentic_de_pipeline_snapshot_age_hours",
            {"h": 5, "w": 6, "x": 12, "y": 52},
            unit="h",
            decimals=1,
            thresholds=[{"color": GOOD, "value": None}, {"color": WARNING, "value": 24}],
        )
    )
    p.append(
        bars(
            "Records failing each expectation (event log)",
            'agentic_de_pipeline_expectation_records{result="failed"}',
            "{{dataset}} - {{expectation}}",
            {"h": 9, "w": 12, "x": 0, "y": 57},
            description="Drops (impossible vitals) and flags (identity conflicts) are expected and explained; a "
            "hard-stop expectation failing would stop the pipeline.",
        )
    )
    p.append(
        bars(
            "Rows per table",
            'agentic_de_pipeline_table_rows{table=~"gold.*|silver.silver_patient_(identity_conflicts|duplicate_candidates)"}',
            "{{table}}",
            {"h": 9, "w": 12, "x": 12, "y": 57},
        )
    )
    for i, panel in enumerate(p, 1):
        panel["id"] = i
    return p


def build() -> dict:
    return {
        "uid": "agentic-de",
        "title": "Agentic DE - human work, guardrails, quality",
        "tags": ["agentic-de"],
        "timezone": "browser",
        "schemaVersion": 39,
        "version": 1,
        "editable": True,
        "refresh": "30s",
        "time": {"from": "now-6h", "to": "now"},
        "panels": panels(),
    }


def render() -> str:
    return json.dumps(build(), indent=2) + "\n"


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(render(), encoding="utf-8", newline="\n")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
