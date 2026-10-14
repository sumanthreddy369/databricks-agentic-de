"""Writes docs/results.md: every measured result and comparison in one place,
generated from the saved reports so the tables can't drift from the data.

    uv run python scripts/build_results_page.py

Sources: docs/comparisons/results/ (pipeline counts, golden-set runs),
docs/eda/ (EDA summary, detector evaluation), ml/models/ (calibration),
and docs/images/stack/ (screenshots of each running stack).
"""

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RESULTS = REPO / "docs" / "comparisons" / "results"
EDA = REPO / "docs" / "eda"
OUT = REPO / "docs" / "results.md"


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _table(header: list[str], rows: list[list]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return lines + [""]


def _pct(x) -> str:
    return "-" if x is None else f"{x:.0%}" if x >= 0.1 or x == 0 else f"{x:.1%}"


def _truth(entry: dict) -> str:
    kind, truth = entry["kind"], entry.get("truth")
    if kind == "refuse":
        return "must refuse"
    if kind == "suppress":
        return "must not reveal (true count < 11)"
    if not truth:
        return "-"
    if kind == "by_group":
        return f"{len(truth)} groups"
    value = next(iter(truth[0].values()))
    return f"{value:.1f}" if isinstance(value, float) else str(value)


MARK = {"correct": "correct", "pass": "pass", "fail": "**fail**", "wrong": "**wrong**", "no_answer": "no answer"}


def pipeline() -> list[str]:
    snap = _load(sorted(RESULTS.glob("workspace_*.json"))[-1])
    out = [
        "## 1. Pipeline on Databricks",
        "",
        f"**Free Edition, {snap['measured_at'][:10]}** - {snap['note']}. Captured by "
        "`scripts/workspace_snapshot.py`: row counts from the tables, expectations from the pipeline's event log.",
        "",
        "![Pipeline lineage](images/pipeline_lineage_free_edition.png)",
        "",
    ]
    out += _table(
        ["Expectation", "Dataset", "Passed", "Failed"],
        [
            [
                f"`{e['expectation']}`",
                e["dataset"],
                f"{e['passed']:,}",
                f"**{e['failed']:,}**" if e["failed"] else "0",
            ]
            for e in snap["expectations"]
        ],
    )
    out += [
        "`silver_contract_checked_events` is a view read by five downstream flows, so its checks count every event "
        "five times (517,035 = 5 x 103,407). `plausible_vital_value` drops the 241 physically impossible readings; "
        "`one_identity_per_patient_id` flags (doesn't drop) the 23 injected two-people-one-id cases.",
        "",
    ]
    m, ident = snap["masks"], snap["identity_check"]
    out += _table(
        ["Check", "Result"],
        [
            [
                "Names / MRNs masked in gold.dim_patients",
                f"{m['names_masked']:,} / {m['mrns_masked']:,} of {m['patients']:,}",
            ],
            [
                "patient_ids flagged as two people on one id",
                f"{ident['flagged_two_people_one_id']} of 23 injected",
            ],
        ],
    )
    first = _load(RESULTS / "pipeline_run1_counts.json")
    out += [
        f"**GCP trial, {first['measured_at']}** (original uniform dataset; that workspace later locked sign-in):",
        "",
    ]
    out += _table(["Table", "Rows"], [[k, f"{v:,}"] for k, v in first["rows"].items()])
    return out + ["![Rows per table](images/pipeline_row_counts.png)", ""]


def golden() -> list[str]:
    run1 = _load(RESULTS / "golden_run1_20261014T020349Z.json")
    run2 = _load(RESULTS / "golden_run2_20261014T021229Z.json")
    rescored = _load(RESULTS / "golden_run2_20261014T021229Z_rescored.json")
    run3 = _load(sorted(RESULTS.glob("golden_run3_*.json"))[-1])
    out = [
        "## 2. Genie: alone vs behind our guardrails vs the metric path",
        "",
        "27 questions on a live Genie space. Each is sent to Genie once; the guarded result is the same answer "
        "passed through `ask_genie`. Truth SQL runs on Gold at evaluation time. Runs 1-2: GCP trial workspace, "
        "original data. Run 3: Databricks Free Edition, regenerated physiology data, current guardrails, live.",
        "",
    ]
    rows = []
    for config, name in (
        ("genie", "Genie alone"),
        ("genie_guarded", "Genie + guardrails"),
        ("metric_path", "Metric path"),
    ):
        cells = [name]
        for report in (run1, run2, rescored, run3):
            s = report["summary"].get(config, {})
            good = s.get("correct", 0) + s.get("pass", 0)
            cells.append(f"{good} / {s.get('questions', 0)}")
        rows.append(cells)
    out += _table(
        ["Configuration (correct + passed probes)", "Run 1", "Run 2", "Run 2 re-scored", "**Run 3 (live)**"], rows
    )
    out += [
        "Runs 1 and 2 are as scored at the time. Run 2's Genie columns include a scorer bug (Genie's PICU refusal used "
        "a typographic apostrophe and was marked a fail); the re-scored column fixes it and applies the guardrails "
        "fixed after run 2. Run 3 ran those guardrails live: Genie alone again reported a group of 1 patient, and "
        "the guard suppressed it and withheld Genie's text.",
        "",
    ]
    rows = []
    for label, report in (("Run 2 (GCP trial)", rescored), ("Run 3 (Free Edition)", run3)):
        lat = report["summary"]
        rows.append(
            [
                label,
                f"{lat['genie_latency_s']['p50']} s / {lat['genie_latency_s']['p90']} s",
                f"{lat['metric_latency_s']['p50']} s",
            ]
        )
    out += _table(["Latency", "Genie p50 / p90", "Metric path p50"], rows)
    out += ["### Every question (run 3, with earlier Genie-alone results)", ""]
    first = {r["id"]: r for r in run1["results"]}
    second = {r["id"]: r for r in rescored["results"]}
    rows = []
    for r in run3["results"]:
        before = first.get(r["id"])
        same = before is not None and before["question"] == r["question"]
        s = r["scores"]
        rows.append(
            [
                f"`{r['id']}`",
                r["question"].replace("|", "/")[:90],
                _truth(r),
                MARK[before["scores"]["genie"]] if same else "-",
                MARK[second[r["id"]]["scores"]["genie"]] if r["id"] in second else "-",
                MARK[s["genie"]],
                MARK[s["genie_guarded"]],
                MARK[s["metric_path"]] if "metric_path" in s else "-",
                f"{r['genie_latency_s']} s",
            ]
        )
    out += _table(
        [
            "Id",
            "Question",
            "Truth (run 3)",
            "Genie run 1",
            "Genie run 2",
            "Genie run 3",
            "Genie + guardrails run 3",
            "Metric path run 3",
            "Genie latency",
        ],
        rows,
    )
    out += [
        "`-` in the run 1 column: the question changed between runs (the SpO2 question was renamed when it got a "
        "metric; the length-of-stay probe was answerable, so later runs ask about blood glucose instead). Truth "
        "differs between runs 1-2 and run 3 because the data was regenerated.",
        "",
        "![Golden set results](images/golden_results.png)",
        "",
        "![Answer latency](images/answer_latency.png)",
        "",
    ]
    return out


def eda() -> list[str]:
    s = _load(EDA / "eda_summary.json")
    c = s["checks"]
    out = ["## 3. Data (EDA of the 103,301 landed events)", ""]
    out += _table(
        ["Check", "Result"],
        [
            ["Duplicate event ids / contents", f"{c['duplicate_event_ids']} / {c['duplicate_contents']}"],
            ["Missing keys", c["missing_keys"]],
            ["Late or out-of-order events", s["late_or_out_of_order_events"]],
            ["Encounters without admission", c["encounters_without_admission"]],
            ["Discharged before admitted", c["discharged_before_admitted"]],
            [
                "Vitals before admission / after discharge",
                f"{c['vitals_before_admission']} / {c['vitals_after_discharge']}",
            ],
            [
                "patient_ids used by two different people",
                f"**{s['patient_identity_drift']['name_changed']} of {s['patient_identity_drift']['readmitted']}**",
            ],
        ],
    )
    out += _table(
        ["Vital", "Mean", "SD", "Median", "Outside alert band", "Physically impossible"],
        [
            [v["itemid"], v["mean"], v["sd"], v["median"], v["outside_alert_band"], v["physically_impossible"]]
            for v in s["vitals"]
        ],
    )
    return out + [
        "![Vital distributions](eda/vital_distributions.png)",
        "",
        "Details: [eda/README.md](eda/README.md)",
        "",
    ]


def detectors() -> list[str]:
    r = _load(EDA / "model_eval.json")
    dets = r["detectors"]
    names = {
        "range_rule": "Range rule",
        "news2": "NEWS2",
        "model_v1": "Model v1 (uniform data)",
        "model_v2": "Model v2 (physiology)",
    }
    out = [
        "## 4. Detecting deterioration: rules vs NEWS2 vs models",
        "",
        "Labelled panels of vitals; each cell is the share flagged. Rows marked *should NOT flag* are false alarms.",
        "",
    ]
    for suite, title in (
        ("physiology", "Realistic simulated patients"),
        ("uniform", "First evaluation (uniform vitals)"),
    ):
        body = r["suites"][suite]
        out += [f"### {title}", ""]
        rows = [
            [name.replace("_", " ") + ("" if row["should_flag"] else " *(should NOT flag)*")]
            + [_pct(row["flag_rate"][d]) for d in dets]
            for name, row in body["sets"].items()
        ]
        rows.append(
            ["**Recall / false-alarm rate**"]
            + [
                f"{_pct(body['summary'][d]['recall'])} / {_pct(body['summary'][d]['false_positive_rate'])}"
                for d in dets
            ]
        )
        rows.append(["ROC AUC"] + [f"{body['summary'][d]['roc_auc']:.2f}" for d in dets])
        out += _table(["Set"] + [names[d] for d in dets], rows)
        if suite == "uniform":
            out += [
                "Here *normal* only means every vital is inside its alert band, drawn uniformly - a heart rate of 135 "
                "counts - so NEWS2 and model v2 flagging it is clinically reasonable. This suite exists to show why "
                "model v1 failed.",
                "",
            ]
    m = r["matched_false_alarms"]
    out += [
        "### Same false-alarm rate",
        "",
        f"Each model's threshold set so it flags as many stable patients as NEWS2 does "
        f"({_pct(m['stable_false_positive_rate'])}):",
        "",
    ]
    rows = [["NEWS2", _pct(r["suites"]["physiology"]["summary"]["news2"]["recall"])]]
    rows += [[names[k], _pct(v["recall"])] for k, v in m["models"].items()]
    out += _table(["Detector", "Deterioration caught"], rows)
    d = r["decision"]
    out += [f"**Decision:** {d['use']}. {d.get('caveat', '')}", ""]
    if "layered" in r:
        lay = r["layered"]
        out += [
            f"**What the agent's tool does** (rules OR NEWS2 OR calibrated model): catches {_pct(lay['recall'])} "
            f"of deteriorating panels with {_pct(lay['stable_false_positive_rate'])} false alarms on stable patients.",
            "",
        ]
    if "landed_stream" in r:
        ls = r["landed_stream"]
        out += [
            f"On the {ls['panels']:,} panels of the landed stream (the uniform data now on Databricks): "
            + ", ".join(f"{names[k]} {_pct(v)}" for k, v in ls["flag_rate"].items())
            + ". Those vitals aren't physiological, so clinical detectors fire constantly on them; the stream "
            "needs regenerating with the new simulator.",
            "",
        ]
    return out + [
        "![Detector comparison](eda/detector_comparison.png)",
        "",
        "![Model v1 vs range rule](eda/model_vs_range_rule.png)",
        "",
    ]


def stacks() -> list[str]:
    shots = [
        (
            "Kafka / Redpanda",
            "redpanda_topic.png",
            "Live patient events in the `patient_events` topic, keyed by encounter (simulator/producer.py).",
        ),
        (
            "MLflow",
            "mlflow_registered_model.jpg",
            "Model v2 training run, registered as `vitals_anomaly_detector`.",
        ),
        (
            "Grafana",
            "grafana_dashboard.png",
            "Human work, guardrails and quality on one dashboard (demo traffic through the real orchestrator; golden-set and model panels are measured results).",
        ),
        ("Prometheus", "prometheus_query.png", "Guardrail and approval metrics as stored, queried directly."),
        (
            "Jaeger",
            "jaeger_trace.jpg",
            "One incident request: each tool call is a span; the quarantine shows `tool.outcome = pending_approval`.",
        ),
    ]
    out = ["## 5. Each stack, running", ""]
    for title, file, caption in shots:
        if (REPO / "docs" / "images" / "stack" / file).exists():
            out += [f"### {title}", "", caption, "", f"![{title}](images/stack/{file})", ""]
    out += [
        "Databricks workspace screenshots (pipeline graph, Genie, Unity Catalog masks) are pending a fresh "
        "workspace login.",
        "",
    ]
    return out


def build() -> str:
    lines = [
        "# Results",
        "",
        "Every measured result and comparison in this project, generated by `scripts/build_results_page.py` from "
        "the saved reports. Synthetic data throughout; nothing here describes real patients.",
        "",
    ]
    for section in (pipeline, golden, eda, detectors, stacks):
        lines += section()
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    OUT.write_text(build(), encoding="utf-8", newline="\n")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
