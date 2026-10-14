"""Exploratory data analysis of the landed patient-event stream (data/landing),
the same 103,301 events the Databricks pipeline ingested.

    uv run --with matplotlib python scripts/eda_landing.py

Writes docs/eda/eda_summary.json (every number in docs/eda/README.md) and
the charts in docs/eda/. The model-vs-rule chart reads
docs/eda/model_eval.json from `python -m ml.evaluate_anomaly_model`.
Runs locally with DuckDB over the raw JSONL - no workspace, no cost.
"""

import json
import sys
from pathlib import Path

import duckdb
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from common.contracts import PHYSIOLOGIC_LIMITS, VITAL_ITEM_TYPES, VITAL_RANGES  # noqa: E402

EVENTS = (REPO / "data" / "landing" / "patient_events").as_posix()
PROVIDERS = (REPO / "data" / "landing" / "providers").as_posix()
OUT = REPO / "docs" / "eda"

# Validated default palette (dataviz reference instance), light mode.
SURFACE, INK, INK_2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
SERIES_1, SERIES_2, BAND = "#2a78d6", "#eb6834", "#f0efec"
plt.rcParams.update(
    {
        "font.family": "DejaVu Sans",
        "font.size": 9.5,
        "axes.edgecolor": GRID,
        "axes.labelcolor": INK_2,
        "xtick.color": INK_2,
        "ytick.color": INK_2,
        "figure.facecolor": SURFACE,
        "axes.facecolor": SURFACE,
        "savefig.facecolor": SURFACE,
    }
)


def _rows(con, sql: str) -> list[dict]:
    rel = con.sql(sql)
    return [dict(zip(rel.columns, r, strict=True)) for r in rel.fetchall()]


def _one(con, sql: str) -> dict:
    return _rows(con, sql)[0]


def summarize(con) -> dict:
    s = {}
    s["volume"] = _one(
        con,
        """SELECT count(*) AS events, count(DISTINCT event_id) AS distinct_event_ids,
           count(DISTINCT patient_id) AS patients, count(DISTINCT encounter_id) AS encounters,
           count(DISTINCT filename) AS files, min(event_ts)::VARCHAR AS first_event,
           max(event_ts)::VARCHAR AS last_event,
           count(DISTINCT schema_version) AS schema_versions FROM ev""",
    )
    s["event_types"] = _rows(con, "SELECT event_type, count(*) AS n FROM ev GROUP BY 1 ORDER BY 2 DESC")
    limits = " UNION ALL ".join(
        f"SELECT '{f}' AS itemid, {VITAL_RANGES[f][0]} AS lo, {VITAL_RANGES[f][1]} AS hi, "
        f"{PHYSIOLOGIC_LIMITS[f][0]} AS plo, {PHYSIOLOGIC_LIMITS[f][1]} AS phi"
        for f in VITAL_ITEM_TYPES
    )
    s["vitals"] = _rows(
        con,
        f"""WITH r AS ({limits})
        SELECT v.vital.itemid AS itemid, any_value(v.vital.valueuom) AS unit, count(*) AS n,
          round(avg(v.vital.value), 2) AS mean, round(stddev(v.vital.value), 2) AS sd,
          min(v.vital.value) AS min, round(quantile_cont(v.vital.value, 0.01), 1) AS p01,
          round(median(v.vital.value), 1) AS median, round(quantile_cont(v.vital.value, 0.99), 1) AS p99,
          max(v.vital.value) AS max,
          sum((v.vital.value < r.lo OR v.vital.value > r.hi)::INT) AS outside_alert_band,
          sum((v.vital.value < r.plo OR v.vital.value > r.phi)::INT) AS physically_impossible
        FROM ev v JOIN r ON r.itemid = v.vital.itemid GROUP BY 1 ORDER BY 1""",
    )
    s["checks"] = _one(
        con,
        """WITH e AS (
            SELECT encounter_id,
              count(*) FILTER (WHERE event_type = 'admitted') AS a,
              count(*) FILTER (WHERE event_type = 'discharged') AS d,
              min(event_ts) FILTER (WHERE event_type = 'admitted') AS adm_ts,
              min(event_ts) FILTER (WHERE event_type = 'discharged') AS dis_ts,
              min(event_ts) FILTER (WHERE event_type = 'vitals_reading') AS first_vital,
              max(event_ts) FILTER (WHERE event_type = 'vitals_reading') AS last_vital
            FROM ev GROUP BY 1)
        SELECT
          (SELECT count(*) - count(DISTINCT event_id) FROM ev) AS duplicate_event_ids,
          (SELECT count(*) - count(DISTINCT (patient_id, event_type, event_ts, vital.itemid, vital.value)) FROM ev)
            AS duplicate_contents,
          (SELECT count(*) FROM ev WHERE patient_id IS NULL OR encounter_id IS NULL OR event_ts IS NULL)
            AS missing_keys,
          (SELECT count(*) FROM ev WHERE event_type = 'transferred' AND transfer.from_unit = transfer.to_unit)
            AS transfers_to_same_unit,
          count(*) FILTER (WHERE a = 0) AS encounters_without_admission,
          count(*) FILTER (WHERE a > 1 OR d > 1) AS encounters_admitted_or_discharged_twice,
          count(*) FILTER (WHERE dis_ts < adm_ts) AS discharged_before_admitted,
          count(*) FILTER (WHERE first_vital < adm_ts) AS vitals_before_admission,
          count(*) FILTER (WHERE last_vital > dis_ts) AS vitals_after_discharge,
          count(*) FILTER (WHERE d = 0) AS still_open,
          round(median(epoch(dis_ts - adm_ts)), 1) AS median_stay_seconds
        FROM e""",
    )
    s["late_or_out_of_order_events"] = _one(
        con,
        """SELECT count(*) AS n FROM (
             SELECT event_ts, max(event_ts) OVER (PARTITION BY filename ORDER BY rn
               ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev
             FROM (SELECT *, row_number() OVER () AS rn FROM ev)) WHERE event_ts < prev""",
    )["n"]
    s["patient_identity_drift"] = _one(
        con,
        """SELECT count(*) AS patients, count(*) FILTER (WHERE encs > 1) AS readmitted,
             count(*) FILTER (WHERE names > 1) AS name_changed,
             count(*) FILTER (WHERE genders > 1) AS gender_changed,
             count(*) FILTER (WHERE regions > 1) AS region_changed,
             count(*) FILTER (WHERE births > 1) AS birth_date_changed
           FROM (SELECT patient_id, count(DISTINCT encounter_id) AS encs,
                   count(DISTINCT patient.full_name) AS names,
                   count(DISTINCT patient.gender) AS genders, count(DISTINCT patient.region) AS regions,
                   count(DISTINCT patient.birth_date) AS births
                 FROM ev GROUP BY 1)""",
    )
    s["admissions_by_unit"] = _rows(
        con,
        "SELECT encounter.unit AS unit, count(*) AS n FROM ev WHERE event_type = 'admitted' GROUP BY 1 ORDER BY 1",
    )
    s["admissions_by_type"] = _rows(
        con,
        "SELECT encounter.encounter_type AS type, count(*) AS n FROM ev WHERE event_type = 'admitted' "
        "GROUP BY 1 ORDER BY 1",
    )
    s["patients_by_age_band"] = _rows(
        con,
        """SELECT
             (date_part('year', age(DATE '2026-10-07', patient.birth_date::DATE)) // 10 * 10)::INT AS age_from,
             count(DISTINCT patient_id) AS n FROM ev WHERE patient IS NOT NULL GROUP BY 1 ORDER BY 1""",
    )
    s["mean_vital_by_unit"] = _rows(
        con,
        """WITH unit_at AS (SELECT encounter_id, any_value(encounter.unit) AS unit FROM ev
                            WHERE event_type = 'admitted' GROUP BY 1)
           SELECT u.unit, v.vital.itemid AS itemid, round(avg(v.vital.value), 2) AS mean
           FROM ev v JOIN unit_at u USING (encounter_id) WHERE v.vital IS NOT NULL GROUP BY 1, 2 ORDER BY 2, 1""",
    )
    s["readings_per_encounter"] = _one(
        con,
        """SELECT round(avg(n), 1) AS mean, min(n) AS min, median(n) AS median, max(n) AS max FROM (
             SELECT encounter_id, count(*) AS n FROM ev WHERE event_type = 'vitals_reading' GROUP BY 1)""",
    )
    # Share of a vital's variance explained by which encounter it belongs to.
    # Near 0 means a "patient" has no persistent physiology in this data.
    s["between_encounter_variance_share"] = _rows(
        con,
        """WITH x AS (SELECT encounter_id, vital.itemid AS itemid, vital.value AS v
                      FROM ev WHERE vital IS NOT NULL),
           m AS (SELECT encounter_id, itemid, avg(v) AS mu, count(*) AS k FROM x GROUP BY 1, 2),
           g AS (SELECT itemid, avg(v) AS gm, var_pop(v) AS tv FROM x GROUP BY 1)
           SELECT m.itemid, round(sum(k * (mu - gm) ^ 2) / sum(k) / any_value(tv), 3) AS share
           FROM m JOIN g USING (itemid) GROUP BY 1 ORDER BY 1""",
    )
    s["providers"] = _one(
        con,
        "SELECT count(*) AS rows, count(DISTINCT provider_id) AS providers, count(DISTINCT filename) AS files "
        "FROM pr",
    )
    return s


def vital_histograms(con) -> None:
    fig, axes = plt.subplots(2, 3, figsize=(10, 5.2))
    for ax, f in zip(axes.ravel(), VITAL_ITEM_TYPES, strict=True):
        values = [r[0] for r in con.sql(f"SELECT vital.value FROM ev WHERE vital.itemid = '{f}'").fetchall()]
        lo, hi = VITAL_RANGES[f]
        ax.axvspan(lo, hi, color=BAND, zorder=0)
        # Values are rounded to 0.1, so bin edges sit on whole multiples of the
        # step: otherwise some bins catch one more 0.1 value than others (a comb).
        width = max(0.1, round((max(values) - min(values)) / 60, 1))
        start = round(min(values) - width / 2, 2)
        edges = [start + i * width for i in range(int((max(values) - start) / width) + 2)]
        ax.hist(values, bins=edges, color=SERIES_1, zorder=2)
        ax.set_title(f, loc="left", color=INK, fontsize=10)
        ax.set_yticks([])
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
    fig.suptitle(
        "Every vital is flat inside its alert band (shaded), with a thin tail outside",
        x=0.01,
        ha="left",
        fontsize=12,
        color=INK,
        fontweight="bold",
    )
    fig.text(
        0.01,
        0.905,
        "99,536 readings from the landed stream. Real vitals cluster around a normal value; these are uniform.",
        fontsize=9.5,
        color=INK_2,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.9))
    fig.savefig(OUT / "vital_distributions.png", dpi=160)
    plt.close(fig)


def model_vs_rule() -> None:
    path = OUT / "model_eval.json"
    if not path.exists():
        return
    report = json.loads(path.read_text(encoding="utf-8"))
    names = {
        "normal": "Normal (should NOT flag)",
        "one_vital_out_of_band": "One vital out of band",
        "diastolic_at_or_above_systolic": "Diastolic >= systolic",
        "respiratory_distress_pattern": "Respiratory-distress pattern",
        "shock_pattern": "Shock pattern",
    }
    # The first evaluation: model v1 (uniform training data) vs the range rule.
    sets = report["suites"]["uniform"]["sets"]
    labels = [names[k] for k in sets]
    model = [sets[k]["flag_rate"]["model_v1"] * 100 for k in sets]
    rule = [sets[k]["flag_rate"]["range_rule"] * 100 for k in sets]
    fig, ax = plt.subplots(figsize=(9, 3.8))
    y = list(range(len(labels)))[::-1]
    h = 0.36
    ax.barh([v + h / 2 for v in y], model, height=h, color=SERIES_1, label="Anomaly model v1 (ONNX)")
    ax.barh([v - h / 2 for v in y], rule, height=h, color=SERIES_2, label="Range rule (Silver already has it)")
    for yi, m, r in zip(y, model, rule, strict=True):
        ax.text(
            m + 1, yi + h / 2, f"{m:.0f}%" if m >= 1 or m == 0 else f"{m:.2f}%", va="center", fontsize=9, color=INK
        )
        ax.text(r + 1, yi - h / 2, f"{r:.0f}%", va="center", fontsize=9, color=INK)
    ax.set_yticks(y, labels, color=INK)
    ax.set_xlim(0, 112)
    ax.set_xlabel("share of test panels flagged")
    ax.grid(axis="x", color=GRID)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.legend(loc="lower right", frameon=False, bbox_to_anchor=(1, 1.0), ncol=2)
    fig.suptitle(
        "Model v1 misses what it was built to catch",
        x=0.01,
        ha="left",
        fontsize=12,
        color=INK,
        fontweight="bold",
    )
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    fig.savefig(OUT / "model_vs_range_rule.png", dpi=160)
    plt.close(fig)


SERIES_3 = "#1baf7a"  # categorical slot 3 (aqua); slots 1-3 validate all-pairs


def detector_comparison() -> None:
    """Simulated patients (simulator/physiology.py): range rule, NEWS2 and
    model v2, per episode, plus false alarms on stable patients."""
    path = OUT / "model_eval.json"
    if not path.exists():
        return
    report = json.loads(path.read_text(encoding="utf-8"))
    sets = report["suites"]["physiology"]["sets"]
    detectors = [
        ("range_rule", "Range rule", SERIES_2),
        ("news2", "NEWS2", SERIES_3),
        ("model_v2", "Model v2 (calibrated)", SERIES_1),
    ]
    order = ["stable"] + [k for k in sets if k != "stable"]
    labels = [
        "Stable (should NOT flag)"
        if k == "stable"
        else k.replace("_", " ").replace(" early", ", early").replace(" established", ", established").capitalize()
        for k in order
    ]
    fig, ax = plt.subplots(figsize=(10, 6.2))
    y = list(range(len(order)))[::-1]
    h = 0.26
    for i, (key, name, color) in enumerate(detectors):
        if key == "model_v2":
            # At the calibrated threshold the agent uses, not the model's default label.
            matched = report["matched_false_alarms"]
            calibrated = matched["models"]["model_v2"]["recall_by_set"]
            calibrated["stable"] = matched["stable_false_positive_rate"]
            values = [calibrated[k] * 100 for k in order]
        else:
            values = [sets[k]["flag_rate"][key] * 100 for k in order]
        offset = (1 - i) * h
        ax.barh([v + offset for v in y], values, height=h, color=color, label=name)
        for yi, v in zip(y, values, strict=True):
            ax.text(v + 0.8, yi + offset, f"{v:.0f}%", va="center", fontsize=7.5, color=INK_2)
    ax.set_yticks(y, labels, color=INK)
    ax.set_xlim(0, 112)
    ax.set_xlabel("share of panels flagged")
    ax.grid(axis="x", color=GRID)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.legend(loc="lower right", frameon=False, bbox_to_anchor=(1, 1.0), ncol=3)
    matched = report["decision"]["recall_at_matched_false_alarms"]
    fig.suptitle(
        "Realistic patients: model v2 catches most, at a calibrated threshold",
        x=0.01,
        ha="left",
        fontsize=12,
        color=INK,
        fontweight="bold",
    )
    fig.text(
        0.01,
        0.935,
        f"At NEWS2's false-alarm rate on stable patients, model v2 catches {matched['model_v2']:.0%} "
        f"of deteriorating "
        f"panels vs NEWS2 {matched['news2']:.0%}. Simulated physiology only.",
        fontsize=9.5,
        color=INK_2,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    fig.savefig(OUT / "detector_comparison.png", dpi=160)
    plt.close(fig)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(
        f"CREATE VIEW ev AS SELECT * FROM read_json_auto('{EVENTS}/*.jsonl', union_by_name=true, filename=true)"
    )
    con.execute(
        f"CREATE VIEW pr AS SELECT * FROM read_json_auto('{PROVIDERS}/*.jsonl', union_by_name=true, filename=true)"
    )
    summary = summarize(con)
    (OUT / "eda_summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    vital_histograms(con)
    model_vs_rule()
    detector_comparison()
    print(json.dumps(summary, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
