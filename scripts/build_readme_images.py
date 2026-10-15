"""Builds the README result images in docs/images/ from measured data.

    uv run --no-project --with matplotlib --with pyyaml python scripts/build_readme_images.py [golden_report.json]

Pipeline counts are the row counts measured on the GCP trial workspace after
the first successful run (2026-10-14). The golden-set charts read a report
written by `python -m evals.golden.run` (evals/reports/golden_*.json); with no
report given, the newest one is used.
"""

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "docs" / "images"

# Validated default palette (dataviz reference instance), light mode.
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e4e3df"
SERIES_1 = "#2a78d6"
GOOD, CRITICAL, NEUTRAL = "#0ca30c", "#d03b3b", "#b9b8b1"

plt.rcParams.update(
    {
        "font.family": "DejaVu Sans",
        "font.size": 10,
        "axes.edgecolor": GRID,
        "axes.labelcolor": INK_2,
        "xtick.color": INK_2,
        "ytick.color": INK,
        "figure.facecolor": SURFACE,
        "axes.facecolor": SURFACE,
        "savefig.facecolor": SURFACE,
    }
)


def _style(ax):
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.tick_params(axis="y", length=0)
    ax.xaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def _hbar(ax, labels, values, note=None):
    y = list(range(len(labels)))[::-1]
    ax.barh(y, values, color=SERIES_1, height=0.55)
    ax.set_yticks(y, labels)
    top = max(values)
    for yi, v in zip(y, values, strict=True):
        ax.text(v + top * 0.01, yi, f"{v:,}", va="center", color=INK, fontsize=9.5)
    ax.set_xlim(0, top * 1.18)
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda x, _: f"{int(x):,}"))
    _style(ax)
    if note:
        ax.set_title(note, loc="left", color=INK_2, fontsize=9.5)


def pipeline_counts():
    fig, (a, b) = plt.subplots(1, 2, figsize=(11, 3.6), gridspec_kw={"width_ratios": [1.15, 1]})
    _hbar(
        a,
        ["Raw events (ingest)", "Bronze", "Silver vitals", "Gold vitals"],
        [103_301, 103_301, 99_213, 99_213],
        "Event rows - 0 lost, 0 corrupt; 323 impossible vitals dropped",
    )
    _hbar(
        b,
        ["Encounter history (SCD2)", "Encounters", "Patients", "Providers"],
        [3_765, 1_401, 1_388, 30],
        "Entity rows - 1 history row per ADT event",
    )
    fig.suptitle(
        "First real pipeline run on Databricks (GCP trial, serverless) - rows per table",
        x=0.01,
        ha="left",
        fontsize=12.5,
        color=INK,
        fontweight="bold",
    )
    fig.tight_layout()
    fig.savefig(OUT / "pipeline_row_counts.png", dpi=160)
    plt.close(fig)


def census_by_unit():
    units = ["ED", "Oncology", "MedSurg", "Cardiology", "ICU"]
    values = [52, 40, 39, 35, 34]
    fig, ax = plt.subplots(figsize=(7.5, 3.2))
    _hbar(ax, units, values, "Same numbers from Genie, compute_metric and direct SQL on Gold")
    fig.suptitle(
        "Current census by unit (open encounters)", x=0.01, ha="left", fontsize=12.5, color=INK, fontweight="bold"
    )
    fig.tight_layout()
    fig.savefig(OUT / "census_by_unit.png", dpi=160)
    plt.close(fig)


OUTCOMES = [("correct", "Correct", GOOD), ("wrong", "Wrong", CRITICAL), ("no_answer", "No answer", NEUTRAL)]
GOV = [("pass", "Pass", GOOD), ("fail", "Fail", CRITICAL)]
CONFIGS = [
    ("genie", "Genie alone"),
    ("genie_guarded", "Genie + our guardrails"),
    ("metric_path", "Metric path (compute_metric)"),
]


def _stacked(ax, rows, categories, title):
    y = list(range(len(rows)))[::-1]
    for yi, (label, counts) in zip(y, rows, strict=True):
        left = 0
        for key, name, color in categories:
            n = counts.get(key, 0)
            if n:
                ax.barh(yi, n, left=left, color=color, height=0.55, edgecolor=SURFACE, linewidth=2)
                ax.text(
                    left + n / 2,
                    yi,
                    f"{n}",
                    ha="center",
                    va="center",
                    color="white",
                    fontsize=9.5,
                    fontweight="bold",
                )
                left += n
        ax.text(left + 0.3, yi, f"{counts.get(categories[0][0], 0)}/{left}", va="center", color=INK, fontsize=9.5)
    ax.set_yticks(y, [r[0] for r in rows])
    ax.set_xlim(0, max(sum(c.values()) for _, c in rows) * 1.2)
    _style(ax)
    ax.set_title(title, loc="left", color=INK_2, fontsize=9.5)
    handles = [plt.Rectangle((0, 0), 1, 1, color=c) for _, _, c in categories]
    ax.legend(
        handles,
        [n for _, n, _ in categories],
        loc="lower right",
        frameon=False,
        fontsize=9,
        ncol=3,
        bbox_to_anchor=(1, 1.08),
    )


def golden_charts(report_path: Path):
    report = json.loads(report_path.read_text(encoding="utf-8"))
    results = report["results"]

    def tally(config, kinds):
        counts = {}
        for r in results:
            if r["kind"] in kinds and config in r["scores"]:
                counts[r["scores"][config]] = counts.get(r["scores"][config], 0) + 1
        return counts

    answer_rows = [(name, tally(key, {"scalar", "by_group"})) for key, name in CONFIGS]
    gov_rows = [(name, tally(key, {"refuse", "suppress"})) for key, name in CONFIGS]
    gov_rows = [r for r in gov_rows if r[1]]
    answer_rows = [r for r in answer_rows if r[1]]

    fig, (a, b) = plt.subplots(
        2, 1, figsize=(9, 5.8), gridspec_kw={"height_ratios": [len(answer_rows), len(gov_rows)]}
    )
    _stacked(a, answer_rows, OUTCOMES, "Answerable questions (truth computed from Gold at run time)")
    _stacked(b, gov_rows, GOV, "Governance probes (names/MRNs, unknown unit, missing data, tiny group)")
    fig.suptitle(
        f"Golden question set on the live workspace ({len(results)} questions)",
        x=0.01,
        ha="left",
        fontsize=12.5,
        color=INK,
        fontweight="bold",
    )
    note = f"run {report['run_at']}"
    if report.get("rescored_from"):
        note += " - the recorded Genie answers, re-scored with the current guardrails"
    fig.text(0.01, 0.925, note, ha="left", fontsize=9.5, color=INK_2)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    fig.savefig(OUT / "golden_results.png", dpi=160)
    plt.close(fig)

    lat = report["summary"]
    labels, values = ["Genie (p50)", "Genie (p90)"], [lat["genie_latency_s"]["p50"], lat["genie_latency_s"]["p90"]]
    if "metric_latency_s" in lat:
        labels.append("Metric path (p50)")
        values.append(lat["metric_latency_s"]["p50"])
    fig, ax = plt.subplots(figsize=(7.5, 2.6))
    y = list(range(len(labels)))[::-1]
    ax.barh(y, values, color=SERIES_1, height=0.55)
    ax.set_yticks(y, labels)
    for yi, v in zip(y, values, strict=True):
        ax.text(v + max(values) * 0.01, yi, f"{v:.1f} s", va="center", color=INK, fontsize=9.5)
    ax.set_xlim(0, max(values) * 1.2)
    _style(ax)
    ax.set_xlabel("seconds per question (same serverless SQL warehouse)")
    fig.suptitle("Answer latency", x=0.01, ha="left", fontsize=12.5, color=INK, fontweight="bold")
    fig.tight_layout()
    fig.savefig(OUT / "answer_latency.png", dpi=160)
    plt.close(fig)


# Warning status, darkened from #fab219 so it reads as text on the light surface.
WARN = "#9a6700"


def pipeline_lineage(snapshot_path: Path):
    """The pipeline as it ran, from a workspace snapshot (scripts/workspace_snapshot.py):
    row counts per table and each expectation's outcome from the event log."""
    snap = json.loads(snapshot_path.read_text(encoding="utf-8"))
    rows = {t["table"].split(".")[-1]: t["rows"] for t in snap["tables"]}
    exp = {(e["dataset"], e["expectation"]): e for e in snap["expectations"]}

    def check(dataset, name, kind):
        e = exp.get((dataset, name))
        if e is None:
            return None
        if e["failed"] == 0:
            return (f"{name}: 0 failed", GOOD)
        verb = {"drop": "dropped", "warn": "flagged", "fail": "failed"}[kind]
        return (f"{name}: {e['failed']:,} {verb}", WARN if kind == "warn" else INK_2)

    # (column, row, title, rows key or text, checks)
    boxes = [
        (0, 0, "raw_patient_events", rows.get("raw_patient_events"), []),
        (
            1,
            0,
            "bronze_patient_events",
            rows.get("bronze_patient_events"),
            [check("bronze_patient_events", "has_event_id", "warn")],
        ),
        (
            2,
            0,
            "contract gate (view)",
            "hard stop on unknown event types",
            [check("silver_contract_checked_events", "known_event_type", "fail")],
        ),
        (
            3,
            -2,
            "silver_fct_vitals",
            rows.get("silver_fct_vitals"),
            [check("silver_fct_vitals", "plausible_vital_value", "drop")],
        ),
        (3, -1, "silver_fct_encounters", rows.get("silver_fct_encounters"), []),
        (3, 0, "silver_fct_encounter_history", rows.get("silver_fct_encounter_history"), []),
        (3, 1, "silver_dim_patients", rows.get("silver_dim_patients"), []),
        (3, 2, "silver_patient_admissions", rows.get("silver_patient_admissions"), []),
        (
            4,
            -2,
            "gold.fct_vitals",
            rows.get("fct_vitals"),
            [(f"-> gold_live_vitals_by_unit: {rows.get('gold_live_vitals_by_unit', 0)} windows", INK_2)],
        ),
        (4, -1, "gold.fct_encounters", rows.get("fct_encounters"), []),
        (4, 0, "gold.fct_encounter_history", rows.get("fct_encounter_history"), []),
        (
            4,
            1,
            "gold.dim_patients",
            rows.get("dim_patients"),
            [(f"names / MRNs masked: {snap['masks']['names_masked']:,} / {snap['masks']['mrns_masked']:,}", GOOD)],
        ),
        (
            4,
            2,
            "silver_patient_identity_conflicts",
            rows.get("silver_patient_identity_conflicts"),
            [check("silver_patient_identity_conflicts", "one_identity_per_patient_id", "warn")],
        ),
        (
            4,
            3,
            "silver_patient_duplicate_candidates",
            rows.get("silver_patient_duplicate_candidates"),
            [check("silver_patient_duplicate_candidates", "one_patient_id_per_person", "warn")],
        ),
        (0, 4.4, "raw_provider_roster", rows.get("raw_provider_roster"), []),
        (1, 4.4, "bronze_provider_roster", rows.get("bronze_provider_roster"), []),
        (3, 4.4, "silver_provider_roster", rows.get("silver_provider_roster"), []),
        (4, 4.4, "gold.dim_providers", rows.get("dim_providers"), []),
    ]
    edges = [((0, 0), (1, 0)), ((1, 0), (2, 0))]
    edges += [((2, 0), (3, y)) for y in (-2, -1, 0, 1, 2)]
    edges += [((3, y), (4, y)) for y in (-2, -1, 0, 1, 2)]
    edges += [((3, 2), (4, 3))]
    edges += [((0, 4.4), (1, 4.4)), ((1, 4.4), (3, 4.4)), ((3, 4.4), (4, 4.4))]

    fig, ax = plt.subplots(figsize=(14, 8.2))
    w, h, xgap = 2.45, 0.78, 2.8
    pos = {}
    for col, row, title, value, checks in boxes:
        x, y = col * xgap, -row
        pos[(col, row)] = (x, y)
        ax.add_patch(
            matplotlib.patches.FancyBboxPatch(
                (x - w / 2, y - h / 2),
                w,
                h,
                boxstyle="round,pad=0.02,rounding_size=0.08",
                facecolor=SURFACE,
                edgecolor=GRID,
                linewidth=1.2,
                zorder=2,
            )
        )
        ax.text(x, y + 0.2, title, ha="center", va="center", fontsize=8.6, color=INK, fontweight="bold", zorder=3)
        detail = f"{value:,} rows" if isinstance(value, int) else (value or "")
        ax.text(x, y - 0.02, detail, ha="center", va="center", fontsize=8.4, color=INK_2, zorder=3)
        for i, c in enumerate(c for c in checks if c):
            ax.text(x, y - 0.24 - i * 0.17, c[0], ha="center", va="center", fontsize=7.6, color=c[1], zorder=3)
    for a, b in edges:
        (x1, y1), (x2, y2) = pos[a], pos[b]
        ax.annotate(
            "",
            xy=(x2 - w / 2, y2),
            xytext=(x1 + w / 2, y1),
            arrowprops={"arrowstyle": "-|>", "color": SERIES_1, "lw": 1.1, "shrinkA": 2, "shrinkB": 2},
            zorder=1,
        )
    for col, label in enumerate(["Ingest (Autoloader)", "Bronze", "Contract gate", "Silver", "Gold / checks"]):
        ax.text(col * xgap, 2.75, label, ha="center", fontsize=9.5, color=INK_2)
    ax.set_xlim(-w / 2 - 0.2, 4 * xgap + w / 2 + 0.2)
    ax.set_ylim(-4.95, 3.0)
    ax.axis("off")
    fig.suptitle(
        "The pipeline as it ran on Databricks Free Edition - rows per table, expectations from the event log",
        x=0.01,
        ha="left",
        fontsize=12.5,
        color=INK,
        fontweight="bold",
    )
    fig.text(0.01, 0.905, snap.get("note", ""), fontsize=9.5, color=INK_2)
    fig.tight_layout(rect=(0, 0, 1, 0.9))
    fig.savefig(OUT / "pipeline_lineage_free_edition.png", dpi=160)
    plt.close(fig)


def main(argv):
    OUT.mkdir(parents=True, exist_ok=True)
    pipeline_counts()
    census_by_unit()
    snapshots = sorted((REPO / "docs" / "comparisons" / "results").glob("workspace_*.json"))
    if snapshots:
        pipeline_lineage(snapshots[-1])
    reports = sorted((REPO / "evals" / "reports").glob("golden_*.json"))
    report = Path(argv[0]) if argv else (reports[-1] if reports else None)
    if report:
        golden_charts(report)
    print("wrote", sorted(p.name for p in OUT.glob("*.png")))


if __name__ == "__main__":
    main(sys.argv[1:])
