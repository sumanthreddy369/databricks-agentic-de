"""Compares deterioration / anomaly detectors on labelled panels of vitals:

- range_rule: any vital outside VITAL_RANGES (what Silver already flags);
- news2:      the NEWS2 early-warning trigger (agent/tools/early_warning.py);
- model_v1:   IsolationForest trained on the original uniform generator
              (ml/models/vitals_anomaly_v1_uniform.onnx);
- model_v2:   IsolationForest trained on stable patients from
              simulator/physiology.py (ml/models/vitals_anomaly.onnx).

Two suites of labelled panels:

- uniform: the first evaluation's sets (vitals drawn uniformly in band),
  kept so the v1 finding stays reproducible;
- physiology: stable patients from every unit (should NOT flag), and each
  deterioration episode at early (severity 0.2-0.5) and established
  (0.6-1.0) severity (should flag).

Plus the landed stream (data/landing, the uniform data on Databricks): how
often each detector fires on real panels.

    uv run python -m ml.evaluate_anomaly_model     # writes docs/eda/model_eval.json

Scoring goes through onnxruntime on the committed artifacts, the same path
the agent's score_vitals_anomaly tool uses.
"""

import json
import random
import sys
from pathlib import Path

import numpy as np
import onnxruntime as ort
from sklearn.metrics import roc_auc_score

from agent.tools.early_warning import news2
from common.contracts import VITAL_ITEM_TYPES, VITAL_RANGES
from simulator.physiology import EPISODES, UNIT_ACUITY, Physiology

REPO_ROOT = Path(__file__).resolve().parent.parent
MODELS = {
    "model_v1": REPO_ROOT / "ml" / "models" / "vitals_anomaly_v1_uniform.onnx",
    "model_v2": REPO_ROOT / "ml" / "models" / "vitals_anomaly.onnx",
}
LANDING = REPO_ROOT / "data" / "landing" / "patient_events"
OUT_PATH = REPO_ROOT / "docs" / "eda" / "model_eval.json"
CALIBRATION_PATH = REPO_ROOT / "ml" / "models" / "vitals_anomaly_calibration.json"
FEATURES = list(VITAL_ITEM_TYPES)
DETECTORS = ["range_rule", "news2", *MODELS]
SEED = 20261014


# --- labelled sets -------------------------------------------------------------------


def _in_band(rng: np.random.Generator, n: int) -> np.ndarray:
    return np.column_stack([rng.uniform(*VITAL_RANGES[f], n) for f in FEATURES])


def _set(x: np.ndarray, name: str, low: float, high: float, rng: np.random.Generator) -> None:
    x[:, FEATURES.index(name)] = rng.uniform(low, high, len(x))


def uniform_sets(rng: np.random.Generator, n_normal: int = 2000, n_each: int = 500) -> dict:
    """{name: (matrix, label)} from the first evaluation; label 1 = should flag."""
    sets = {"normal": (_in_band(rng, n_normal), 0)}
    oor = _in_band(rng, n_each)
    for i, c in enumerate(rng.integers(0, len(FEATURES), n_each)):
        low, high = VITAL_RANGES[FEATURES[c]]
        push = rng.uniform(0.1, (high - low) * 0.5)
        oor[i, c] = low - push if rng.random() < 0.5 else high + push
    sets["one_vital_out_of_band"] = (oor, 1)
    pair = _in_band(rng, n_each)
    sbp = rng.uniform(85, 100, n_each)
    pair[:, FEATURES.index("sbp")] = sbp
    pair[:, FEATURES.index("dbp")] = rng.uniform(sbp, 100)
    sets["diastolic_at_or_above_systolic"] = (pair, 1)
    distress = _in_band(rng, n_each)
    _set(distress, "heart_rate", 125, 140, rng)
    _set(distress, "spo2", 88, 90, rng)
    _set(distress, "resp_rate", 26, 30, rng)
    sets["respiratory_distress_pattern"] = (distress, 1)
    shock = _in_band(rng, n_each)
    _set(shock, "sbp", 85, 92, rng)
    _set(shock, "dbp", 50, 55, rng)
    _set(shock, "heart_rate", 125, 140, rng)
    sets["shock_pattern"] = (shock, 1)
    return sets


def physiology_sets(seed: int, n_stable: int = 3000, n_each: int = 500) -> dict:
    rng = random.Random(seed)
    units = sorted(UNIT_ACUITY)

    def panels(n, state=None, severity=(0.0, 0.0)):
        rows = []
        for _ in range(n):
            patient = Physiology.new(rng.choice(units), rng)
            if state:
                patient.state, patient.severity = state, rng.uniform(*severity)
            p = patient.panel(rng)
            rows.append([p[f] for f in FEATURES])
        return np.array(rows, dtype=np.float64)

    sets = {"stable": (panels(n_stable), 0)}
    for episode in sorted(EPISODES):
        sets[f"{episode}_early"] = (panels(n_each, episode, (0.2, 0.5)), 1)
        sets[f"{episode}_established"] = (panels(n_each, episode, (0.6, 1.0)), 1)
    return sets


# --- detectors -----------------------------------------------------------------------


def range_rule(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    low = np.array([VITAL_RANGES[f][0] for f in FEATURES])
    high = np.array([VITAL_RANGES[f][1] for f in FEATURES])
    outside = (x < low) | (x > high)
    return outside.any(axis=1), outside.sum(axis=1).astype(float)


def news2_rule(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    results = [news2(dict(zip(FEATURES, row, strict=True))) for row in x]
    return np.array([r["trigger"] for r in results]), np.array([r["score"] for r in results], dtype=float)


def onnx_model(path: Path):
    session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])

    def detect(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        label, raw = session.run(None, {session.get_inputs()[0].name: x.astype(np.float32)})
        return label.ravel() == -1, -raw.ravel()

    return detect


def detectors(models: dict = MODELS) -> dict:
    out = {"range_rule": range_rule, "news2": news2_rule}
    out.update({name: onnx_model(path) for name, path in models.items() if path.exists()})
    return out


# --- scoring -------------------------------------------------------------------------


def _rate(flags: np.ndarray) -> float:
    return round(float(flags.mean()), 4) if len(flags) else 0.0


def evaluate_suite(sets: dict, dets: dict) -> dict:
    rows, labels, flags, scores = {}, [], {d: [] for d in dets}, {d: [] for d in dets}
    for name, (x, label) in sets.items():
        rows[name] = {"n": len(x), "should_flag": bool(label), "flag_rate": {}}
        labels.append(np.full(len(x), label))
        for det, fn in dets.items():
            f, s = fn(x)
            rows[name]["flag_rate"][det] = _rate(f)
            flags[det].append(f)
            scores[det].append(s)
    y = np.concatenate(labels)
    summary = {}
    for det in dets:
        f, s = np.concatenate(flags[det]), np.concatenate(scores[det])
        tp, fp = int((f & (y == 1)).sum()), int((f & (y == 0)).sum())
        fn_, tn = int((~f & (y == 1)).sum()), int((~f & (y == 0)).sum())
        summary[det] = {
            "recall": round(tp / (tp + fn_), 4) if tp + fn_ else None,
            "false_positive_rate": round(fp / (fp + tn), 4) if fp + tn else None,
            "precision": round(tp / (tp + fp), 4) if tp + fp else None,
            "roc_auc": round(float(roc_auc_score(y, s)), 4),
        }
    return {"sets": rows, "summary": summary}


def landed_panels(landing: Path = LANDING) -> np.ndarray:
    """Per encounter and minute, the latest value of each vital; complete panels only."""
    import duckdb

    pivot = ", ".join(
        f"max_by(vital.value, event_ts) FILTER (WHERE vital.itemid = '{f}') AS {f}" for f in FEATURES
    )
    sql = f"""
        SELECT {pivot} FROM read_json_auto('{landing.as_posix()}/*.jsonl', union_by_name=true)
        WHERE event_type = 'vitals_reading'
        GROUP BY encounter_id, time_bucket(INTERVAL 1 minute, event_ts)
        HAVING {" AND ".join(f"{f} IS NOT NULL" for f in FEATURES)}
    """
    return np.array(duckdb.sql(sql).fetchall(), dtype=np.float64)


def matched_fpr(sets: dict, dets: dict, reference: str = "news2") -> dict:
    """Each model's recall when its threshold is set so it raises exactly as
    many false alarms on stable patients as `reference` does - the fair
    comparison (the ONNX label's built-in threshold is set by training
    contamination, not by any clinical choice)."""
    stable_x = sets["stable"][0]
    target = float(dets[reference](stable_x)[0].mean())
    out = {"reference": reference, "stable_false_positive_rate": round(target, 4), "models": {}}
    for det, fn in dets.items():
        if not det.startswith("model"):
            continue
        threshold = float(np.quantile(fn(stable_x)[1], 1 - target))
        per_set = {
            name: round(float((fn(x)[1] > threshold).mean()), 4) for name, (x, label) in sets.items() if label
        }
        out["models"][det] = {
            "score_threshold": round(threshold, 6),
            "recall_by_set": per_set,
            "recall": round(float(np.mean(list(per_set.values()))), 4),
        }
    return out


def layered(sets: dict, dets: dict, model: str, threshold: float) -> dict:
    """What agent/tools/anomaly_score.py does: flag if the range rule OR NEWS2
    OR the model at its calibrated threshold fires."""
    rates = {}
    for name, (x, _label) in sets.items():
        flags = dets["range_rule"](x)[0] | dets["news2"](x)[0] | (dets[model](x)[1] > threshold)
        rates[name] = round(float(flags.mean()), 4)
    deteriorating = [rate for name, rate in rates.items() if name != "stable"]
    return {
        "flag_rate": rates,
        "recall": round(float(np.mean(deteriorating)), 4),
        "stable_false_positive_rate": rates["stable"],
    }


def decide(physiology: dict, matched: dict) -> dict:
    """Promote a model only if, at NEWS2's false-alarm rate on stable patients,
    it catches more deterioration than NEWS2. Promoted means 'an extra signal
    at a calibrated threshold', never 'replaces the rules'."""
    news_recall = physiology["summary"]["news2"]["recall"]
    models = matched["models"]
    if not models:
        return {"use": "rules + NEWS2", "why": "no model artifact"}
    best = max(models, key=lambda m: models[m]["recall"])
    promoted = models[best]["recall"] > news_recall
    return {
        "use": f"rules + NEWS2 + {best} (calibrated)" if promoted else "rules + NEWS2; model advisory",
        "model": best,
        "promoted": promoted,
        "recall_at_matched_false_alarms": {"news2": news_recall, best: models[best]["recall"]},
        "score_threshold": models[best]["score_threshold"],
        "caveat": "Trained and tested on simulated physiology only; not validated on real patients.",
    }


def evaluate(models: dict = MODELS, landing: Path | None = LANDING, seed: int = SEED) -> dict:
    dets = detectors(models)
    phys_sets = physiology_sets(seed)
    report = {
        "detectors": list(dets),
        "seed": seed,
        "suites": {
            "uniform": evaluate_suite(uniform_sets(np.random.default_rng(seed)), dets),
            "physiology": evaluate_suite(phys_sets, dets),
        },
    }
    report["matched_false_alarms"] = matched_fpr(phys_sets, dets)
    report["decision"] = decide(report["suites"]["physiology"], report["matched_false_alarms"])
    if report["decision"].get("promoted"):
        report["layered"] = layered(
            phys_sets, dets, report["decision"]["model"], report["decision"]["score_threshold"]
        )
    if landing is not None and landing.exists():
        panels = landed_panels(landing)
        report["landed_stream"] = {
            "panels": len(panels),
            "flag_rate": {det: _rate(fn(panels)[0]) for det, fn in dets.items()},
        }
    return report


def main(argv: list[str] | None = None) -> int:
    report = evaluate()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    decision = report["decision"]
    if decision.get("model") == "model_v2":
        # Read by agent/tools/anomaly_score.py: the threshold this evaluation justified.
        CALIBRATION_PATH.write_text(
            json.dumps(
                {
                    "promoted": decision["promoted"],
                    "score_threshold": decision["score_threshold"],
                    "matched_to": "NEWS2 false-alarm rate on stable simulated patients",
                    "stable_false_positive_rate": report["matched_false_alarms"]["stable_false_positive_rate"],
                    "recall_at_matched_false_alarms": decision["recall_at_matched_false_alarms"],
                    "caveat": decision["caveat"],
                    "seed": report["seed"],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    for suite, body in report["suites"].items():
        print(f"\n[{suite}]")
        print(f"{'set':<36}" + "".join(f"{d:>12}" for d in report["detectors"]))
        for name, row in body["sets"].items():
            mark = "(should flag)" if row["should_flag"] else "(should NOT)"
            rates = "".join(f"{row['flag_rate'][d]:>12.3f}" for d in report["detectors"])
            print(f"{name + ' ' + mark:<36}{rates}")
        for d, m in body["summary"].items():
            print(f"  {d:<11} recall {m['recall']}  FPR {m['false_positive_rate']}  AUC {m['roc_auc']}")
    print("\ndecision:", json.dumps(report["decision"]))
    if "landed_stream" in report:
        print("landed stream:", json.dumps(report["landed_stream"]))
    print(f"wrote {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
