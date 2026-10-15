"""Scores three ways of answering "which problem is this, and what do I do?"
on evals/runbook/questions.yaml (30 on-call questions in plain words):

1. keyword lookup - the agent's lookup_problem tool (agent/tools/problem_catalog.py);
2. embedding search - runbook pages nearest the question (agent/runbook_search.py);
3. hybrid - reciprocal rank fusion of 1 and 2;
4. hybrid + a hosted model answering from the top five pages.

A question counts as right when the top result (or the answer's
'Problem <ID>:') is one of its expected IDs; "top 3" when any of the first
three is.

    uv run python scripts/runbook_eval.py --profile free [--model databricks-qwen35-122b-a10b]

Writes docs/comparisons/results/runbook_eval_<date>.json.
"""

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from agent.databricks_llm import DEFAULT_ENDPOINT, DatabricksModel  # noqa: E402
from agent.runbook_search import (  # noqa: E402
    CACHE_PATH,
    RunbookIndex,
    cited_problem,
    databricks_embedder,
    load_pages,
)
from agent.tools.problem_catalog import search  # noqa: E402

QUESTIONS = REPO / "evals" / "runbook" / "questions.yaml"
OUT_DIR = REPO / "docs" / "comparisons" / "results"


def evaluate(questions: list[dict], index: RunbookIndex, model, pause: float = 0.0) -> dict:
    rows = []
    for item in questions:
        time.sleep(pause)  # the free endpoints rate-limit bursts
        expect = set(item["expect"])
        keyword = [p["id"] for p in search(item["q"], limit=3)]
        embedded = [pid for pid, _ in index.search(item["q"], k=3)]
        hybrid = index.hybrid(item["q"], k=3)
        answer, _ = index.answer(item["q"], model) if model is not None else ("", [])
        cited = cited_problem(answer)
        rows.append(
            {
                "q": item["q"],
                "expect": sorted(expect),
                "keyword_top3": keyword,
                "embedding_top3": embedded,
                "hybrid_top3": hybrid,
                "answer_cites": cited,
            }
        )

    def rate(key, first_only):
        hits = 0
        for r in rows:
            got = r[key] if isinstance(r[key], list) else [r[key]]
            got = got[:1] if first_only else got[:3]
            hits += bool(set(got) & set(r["expect"]))
        return hits

    n = len(rows)
    summary = {
        "keyword_lookup": {"top1": rate("keyword_top3", True), "top3": rate("keyword_top3", False)},
        "embedding_search": {"top1": rate("embedding_top3", True), "top3": rate("embedding_top3", False)},
        "hybrid_search": {"top1": rate("hybrid_top3", True), "top3": rate("hybrid_top3", False)},
    }
    if model is not None:
        summary["hybrid_plus_model"] = {"top1": rate("answer_cites", True)}
    return {"questions": n, "summary": summary, "rows": rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--profile", default=None, help="Databricks CLI profile for the hosted models")
    parser.add_argument("--model", default=DEFAULT_ENDPOINT, help="chat endpoint for the answer step")
    parser.add_argument("--no-answer", action="store_true", help="retrieval only")
    parser.add_argument(
        "--questions", default=str(QUESTIONS), help="question file (e.g. evals/runbook/holdout.yaml)"
    )
    parser.add_argument("--label", default="", help="suffix for the output file name")
    args = parser.parse_args(argv)
    questions = yaml.safe_load(Path(args.questions).read_text(encoding="utf-8"))["questions"]
    index = RunbookIndex(load_pages(), databricks_embedder(args.profile), cache_path=CACHE_PATH)
    model = None if args.no_answer else DatabricksModel(args.model, profile=args.profile)
    report = evaluate(questions, index, model, pause=3.0)
    report.update(
        {
            "embedding_endpoint": "databricks-gte-large-en",
            "answer_endpoint": None if model is None else args.model,
            "runbooks": len(index.pages),
            "workspace": "Databricks Free Edition",
            "question_file": Path(args.questions).name,
        }
    )
    out = OUT_DIR / f"runbook_eval_{datetime.now(UTC):%Y%m%d}{'_' + args.label if args.label else ''}.json"
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    for method, scores in report["summary"].items():
        print(method, {k: f"{v}/{report['questions']}" for k, v in scores.items()})
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
