"""Runs the golden question set against the live workspace and scores three
configurations: Genie alone, Genie behind this project's guardrails, and the
deterministic metrics-layer path (compute_metric).

Needs a live workspace: DATABRICKS_HOST, DATABRICKS_TOKEN (e.g. from
`databricks auth token`), GENIE_SPACE_ID, DATABRICKS_WAREHOUSE_ID.

    uv run python -m evals.golden.run

Each question is sent to Genie ONCE. The guarded configuration replays that
same Genie response through ask_genie's guardrails, so the two Genie
configurations differ only by this project's layer, and cost isn't doubled.
Writes evals/reports/golden_<timestamp>.json and prints a summary.
"""

import json
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import yaml

from agent.databricks_client import DatabricksClient, DatabricksError, load_config
from agent.tools.genie import ask_genie
from agent.tools.metrics import compute_metric
from evals.golden.scoring import score_answer

HERE = Path(__file__).resolve().parent
REPORTS = HERE.parent / "reports"


class _Replay:
    """Stands in for DatabricksClient inside ask_genie: returns the Genie
    answer already fetched, so guardrails are applied to the identical
    response and Genie isn't called twice."""

    def __init__(self, answer: dict | None, error: str | None):
        self.answer, self.error = answer, error

    def genie_ask(self, question, **kwargs):
        if self.error:
            raise DatabricksError(self.error)
        return self.answer


def _guarded(question: str, answer: dict | None, error: str | None) -> dict:
    payload = json.loads(ask_genie(question, client=_Replay(answer, error)).content)
    rows = payload.get("rows") or []
    suppressed = payload.get("guardrails", {}).get("suppressed_cells", 0) > 0 or any(
        k.endswith("_suppressed") for r in rows for k in r
    )
    refused = bool(payload.get("refused") or payload.get("withheld"))
    text = payload.get("answer_text") or payload.get("reason") or ""
    return {
        "rows": rows,
        "text": text,
        "refused": refused,
        "suppressed": suppressed,
        "error": payload.get("error"),
    }


def _metric_path(spec: dict) -> dict:
    payload = json.loads(
        compute_metric(
            spec["id"], group_by=spec.get("group_by"), filters=spec.get("filters"), as_of=spec.get("as_of")
        ).content
    )
    if "error" in payload:
        return {"rows": None, "text": payload["error"], "refused": True, "suppressed": False, "error": None}
    rows = payload.get("rows") or []
    suppressed = any(r.get("suppressed") for r in rows)
    clean = [{k: v for k, v in r.items() if k != "suppressed"} for r in rows]
    if suppressed:
        clean = []
    return {"rows": clean, "text": "", "refused": False, "suppressed": suppressed, "error": None}


def summarize(results: list[dict]) -> dict:
    summary = {}
    for config_name in ("genie", "genie_guarded", "metric_path"):
        scored = [r["scores"][config_name] for r in results if config_name in r["scores"]]
        summary[config_name] = dict(Counter(scored))
        summary[config_name]["questions"] = len(scored)
    latencies = sorted(r["genie_latency_s"] for r in results)
    summary["genie_latency_s"] = {
        "p50": latencies[len(latencies) // 2],
        "p90": latencies[int(len(latencies) * 0.9) - 1],
        "max": latencies[-1],
    }
    metric_lat = sorted(r["metric_latency_s"] for r in results if "metric_latency_s" in r)
    if metric_lat:
        summary["metric_latency_s"] = {"p50": metric_lat[len(metric_lat) // 2], "max": metric_lat[-1]}
    return summary


def main() -> int:
    config = load_config()
    if config is None or not config.genie_space_id or not config.warehouse_id:
        print("needs DATABRICKS_HOST, DATABRICKS_TOKEN, GENIE_SPACE_ID, DATABRICKS_WAREHOUSE_ID", file=sys.stderr)
        return 2
    client = DatabricksClient(config, timeout=120)
    questions = yaml.safe_load((HERE / "questions.yaml").read_text(encoding="utf-8"))["questions"]

    results = []
    for q in questions:
        truth = client.execute_statement(q["truth"]) if q.get("truth") else None
        tolerance = q.get("tolerance", 0.0)

        t = time.time()
        try:
            raw, raw_error = client.genie_ask(q["question"], timeout_s=120), None
        except DatabricksError as exc:
            raw, raw_error = None, str(exc)
        genie_latency = time.time() - t

        genie = {
            "rows": (raw or {}).get("rows") or [],
            "text": (raw or {}).get("text") or "",
            "refused": False,
            "suppressed": False,
            "error": raw_error,
        }
        guarded = _guarded(q["question"], raw, raw_error)
        entry = {
            "id": q["id"],
            "question": q["question"],
            "kind": q["kind"],
            "truth": truth,
            "genie_sql": (raw or {}).get("sql"),
            "genie_latency_s": round(genie_latency, 1),
            "scores": {
                "genie": score_answer(q["kind"], truth, genie, tolerance),
                "genie_guarded": score_answer(q["kind"], truth, guarded, tolerance),
            },
            "answers": {"genie": genie, "genie_guarded": guarded},
        }
        if q.get("metric"):
            t = time.time()
            metric = _metric_path(q["metric"])
            entry["metric_latency_s"] = round(time.time() - t, 1)
            entry["scores"]["metric_path"] = score_answer(q["kind"], truth, metric, tolerance)
            entry["answers"]["metric_path"] = metric
        results.append(entry)
        print(f"{q['id']:<28} {json.dumps(entry['scores'])}  genie {entry['genie_latency_s']}s", flush=True)

    summary = summarize(results)
    REPORTS.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report = REPORTS / f"golden_{stamp}.json"
    report.write_text(
        json.dumps({"run_at": stamp, "summary": summary, "results": results}, indent=2, default=str),
        encoding="utf-8",
    )
    print("\nSUMMARY", json.dumps(summary, indent=2))
    print(f"report: {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
