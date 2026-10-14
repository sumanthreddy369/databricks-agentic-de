"""Re-scores a saved golden-set report with the current guardrails and scorer,
without calling Genie again.

Genie's answers vary between runs, so re-asking can't show whether a
guardrail change helped: the case that exposed the gap may not recur. This
replays each recorded Genie response (SQL, rows, text) through the current
ask_genie guardrails and re-scores every configuration. The metric path is
re-scored from its recorded answer, not re-run.

    uv run python -m evals.golden.rescore evals/reports/golden_<stamp>.json

Writes golden_<stamp>_rescored.json next to the input, with "rescored_from"
set, so it is never mistaken for a fresh run.
"""

import json
import sys
from pathlib import Path

import yaml

from evals.golden.run import HERE, _guarded, summarize
from evals.golden.scoring import score_answer


def rescore(report: dict) -> dict:
    questions = {
        q["id"]: q for q in yaml.safe_load((HERE / "questions.yaml").read_text(encoding="utf-8"))["questions"]
    }
    results = []
    for entry in report["results"]:
        q = questions.get(entry["id"], {})
        tolerance = q.get("tolerance", 0.0)
        genie = entry["answers"]["genie"]
        raw = None
        if not genie.get("error"):
            raw = {"sql": entry.get("genie_sql"), "rows": genie.get("rows"), "text": genie.get("text")}
        guarded = _guarded(entry["question"], raw, genie.get("error"))
        new = dict(entry, answers=dict(entry["answers"], genie_guarded=guarded))
        new["scores"] = {
            "genie": score_answer(entry["kind"], entry["truth"], genie, tolerance),
            "genie_guarded": score_answer(entry["kind"], entry["truth"], guarded, tolerance),
        }
        if "metric_path" in entry["answers"]:
            new["scores"]["metric_path"] = score_answer(
                entry["kind"], entry["truth"], entry["answers"]["metric_path"], tolerance
            )
        results.append(new)
    return {
        "run_at": report["run_at"],
        "rescored_from": report["run_at"],
        "summary": summarize(results),
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    source = Path(argv[0])
    out = rescore(json.loads(source.read_text(encoding="utf-8")))
    target = source.with_name(source.stem + "_rescored.json")
    target.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    for before, after in zip(json.loads(source.read_text(encoding="utf-8"))["results"], out["results"]):
        if before["scores"] != after["scores"]:
            print(f"{after['id']:<28} {json.dumps(before['scores'])} -> {json.dumps(after['scores'])}")
    print("\nSUMMARY", json.dumps(out["summary"], indent=2))
    print(f"report: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
