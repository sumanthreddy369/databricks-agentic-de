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


def main(argv):
    OUT.mkdir(parents=True, exist_ok=True)
    pipeline_counts()
    census_by_unit()
    reports = sorted((REPO / "evals" / "reports").glob("golden_*.json"))
    report = Path(argv[0]) if argv else (reports[-1] if reports else None)
    if report:
        golden_charts(report)
    print("wrote", sorted(p.name for p in OUT.glob("*.png")))


if __name__ == "__main__":
    main(sys.argv[1:])
