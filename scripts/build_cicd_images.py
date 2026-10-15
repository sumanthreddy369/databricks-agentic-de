"""Draws the two CI/CD images that aren't GitHub screenshots
(scripts/capture_cicd_screenshots.ps1 takes those), into docs/images/cicd/:

- 00_before_after.png: what the CI/CD pipeline was vs what it is now;
- 09_cd_databricks.png: the CD run on Databricks Free Edition, stage by stage
  (docs/comparisons/results/cd_run_free_edition_20261015.json), and the
  retry bug it found, before and after the fix.

    uv run python scripts/build_cicd_images.py
"""

import json
import sys
import textwrap
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from build_readme_images import CRITICAL, GOOD, GRID, INK, INK_2, SURFACE, WARN  # noqa: E402

OUT = REPO / "docs" / "images" / "cicd"
RESULTS = REPO / "docs" / "comparisons" / "results"

BEFORE_AFTER = [
    ("CI jobs", "1 (lint + tests in one job)", "5 staged: lint -> guardrails + tests -> integration; docs-in-sync"),
    ("Tests per push", "487", "516, guardrail tests as their own job"),
    ("Data-quality gate", "none", "simulated feed with planted defects; fails if any is missed"),
    ("Generated docs", "could drift silently", "results page and 104 runbooks regenerated; fail on diff"),
    ("Bundle stage", "validate only - and it always skipped", "validate, deploy on merge (opt-in), manual integration run"),
    ("Skips", "green with no explanation", "written to the run summary"),
    ("Action pinning", "tags (@v4, @main)", "full commit SHAs + version comment, enforced by a test"),
    ("Workflow files", "not checked", "actionlint (checksum-verified download)"),
    ("Dependency updates", "none", "Dependabot, 14-day cooldown; 4 vetted updates merged"),
    ("Runtime warnings", "Node.js 20 deprecation on every job", "none (actions on Node 24)"),
    ("CD on Databricks", "never deployed by the pipeline", "full run on Free Edition; found a retry that swallowed alerts"),
]


def before_after() -> None:
    fig, ax = plt.subplots(figsize=(14, 7.6))
    ax.axis("off")
    cols = [0.0, 0.2, 0.47]
    y = 0.95
    for x, head in zip(cols, ["", "Before", "Now"]):
        ax.text(x, y, head, fontsize=12, fontweight="bold", color=INK, transform=ax.transAxes)
    y -= 0.045
    ax.plot([0, 1], [y, y], color=GRID, lw=1.2, transform=ax.transAxes)
    for label, before, after in BEFORE_AFTER:
        y -= 0.078
        ax.text(cols[0], y, label, fontsize=10.5, fontweight="bold", color=INK, transform=ax.transAxes, va="center")
        ax.text(cols[1], y, textwrap.fill(before, 32), fontsize=10, color=CRITICAL, transform=ax.transAxes, va="center")
        ax.text(cols[2], y, textwrap.fill(after, 70), fontsize=10, color=GOOD, transform=ax.transAxes, va="center")
        ax.plot([0, 1], [y - 0.039, y - 0.039], color=GRID, lw=0.6, transform=ax.transAxes)
    fig.suptitle("CI/CD: what it was vs what it is now", x=0.02, ha="left", fontsize=14, fontweight="bold", color=INK)
    fig.savefig(OUT / "00_before_after.png", dpi=140, facecolor=SURFACE, bbox_inches="tight")
    plt.close(fig)


def cd_databricks() -> None:
    run = json.loads((RESULTS / "cd_run_free_edition_20261015.json").read_text(encoding="utf-8"))
    stages = run["stages"]
    fig = plt.figure(figsize=(14, 9.4))
    ax = fig.add_axes([0.02, 0.36, 0.96, 0.56])
    ax.axis("off")
    n = len(stages)
    for i, stage in enumerate(stages):
        y = 1 - (i + 0.5) / n
        bug = stage["stage"].startswith("Bug")
        color = WARN if bug else GOOD
        ax.plot([0.015], [y], "o", markersize=11, color=color, transform=ax.transAxes)
        title = textwrap.fill(stage["stage"], 30)
        ax.text(0.04, y, title, fontsize=10.5, fontweight="bold", color=INK, va="center", transform=ax.transAxes)
        ax.text(0.33, y, textwrap.fill(stage["result"], 112), fontsize=9, color=INK_2, va="center", transform=ax.transAxes)
    fig.suptitle(
        "CD on Databricks Free Edition - deploy, run, verify, stop", x=0.02, ha="left", fontsize=14,
        fontweight="bold", color=INK,
    )
    fig.text(0.02, 0.925, run["how"], fontsize=9, color=INK_2)

    # The retry bug: the same deliberate failure, before and after the fix.
    bx = fig.add_axes([0.17, 0.04, 0.8, 0.26])
    rows = [
        ("Before the fix\nrun 555522090914382", [("attempt 1: FAILED (new problem = alert)", CRITICAL),
                                                ("attempt 2 (automatic retry): SUCCESS", GOOD)],
         "run shows SUCCESS - no failure email, alert lost"),
        ("After the fix\nrun 428177390065951", [("attempt 1: FAILED (new problem = alert)", CRITICAL)],
         "run shows FAILED - failure email sent, no retry"),
    ]
    for r, (label, attempts, outcome) in enumerate(rows):
        y = 1 - r
        bx.text(-0.02, y, label, ha="right", va="center", fontsize=10, color=INK, fontweight="bold")
        for a, (text, color) in enumerate(attempts):
            bx.barh(y, 0.9, left=a * 1.0, height=0.5, color=color, alpha=0.18, edgecolor=color)
            bx.text(a * 1.0 + 0.45, y, text, ha="center", va="center", fontsize=9, color=INK)
        bx.text(2.05, y, outcome, va="center", fontsize=10, color=CRITICAL if r == 0 else GOOD, fontweight="bold")
    bx.set_xlim(0, 3.2)
    bx.set_ylim(-0.6, 1.6)
    bx.axis("off")
    bx.set_title(
        "Serverless retried a deliberately failed run; max_retries: 0 + disable_auto_optimization: true stopped it",
        loc="left", fontsize=10.5, color=INK,
    )
    fig.savefig(OUT / "09_cd_databricks.png", dpi=140, facecolor=SURFACE)
    plt.close(fig)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    before_after()
    cd_databricks()
    print("wrote", sorted(p.name for p in OUT.glob("0*.png")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
