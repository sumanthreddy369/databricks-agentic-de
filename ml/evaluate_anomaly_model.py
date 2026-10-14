"""Evaluates the committed vitals anomaly model (ml/models/vitals_anomaly.onnx)
against labelled cases, and against the plain range rule it is meant to
improve on.

Training (ml/train_anomaly_model.py) only logs the fraction of a holdout the
model flags - which `contamination` fixes at about 5% - so it never measured
whether the model catches anything. This does:

- normal panels: every vital inside its alert band (VITAL_RANGES), drawn the
  way the simulator draws them;
- one vital out of band: the simulator's own outlier mechanism;
- joint anomalies with every value in band, which is the case the model
  exists for (agent/tools/anomaly_score.py):
    * diastolic >= systolic - physically impossible as a pair;
    * respiratory-distress pattern - high heart rate, low SpO2, high
      respiratory rate together;
    * shock pattern - low blood pressure with a high heart rate.

Baseline: the range rule (any vital outside VITAL_RANGES), which Silver
already applies. The model is worth deploying only if it beats that.

It also scores real panels from the landed stream (data/landing), built per
encounter and minute from the latest reading of each vital.

    uv run python -m ml.evaluate_anomaly_model            # writes docs/eda/model_eval.json

Scoring goes through onnxruntime on the committed artifact, the same path the
agent's score_vitals_anomaly tool uses.
"""

import json
import sys
from pathlib import Path

import numpy as np
import onnxruntime as ort
from sklearn.metrics import roc_auc_score

from common.contracts import VITAL_ITEM_TYPES, VITAL_RANGES

REPO_ROOT = Path(__file__).resolve().parent.parent
MODEL_PATH = REPO_ROOT / "ml" / "models" / "vitals_anomaly.onnx"
LANDING = REPO_ROOT / "data" / "landing" / "patient_events"
OUT_PATH = REPO_ROOT / "docs" / "eda" / "model_eval.json"
FEATURES = list(VITAL_ITEM_TYPES)
SEED = 20261014


def _in_band(rng: np.random.Generator, n: int) -> np.ndarray:
    return np.column_stack([rng.uniform(*VITAL_RANGES[f], n) for f in FEATURES])


def _set(x: np.ndarray, name: str, low: float, high: float, rng: np.random.Generator) -> None:
    x[:, FEATURES.index(name)] = rng.uniform(low, high, len(x))


def test_sets(
    rng: np.random.Generator, n_normal: int = 2000, n_each: int = 500
) -> dict[str, tuple[np.ndarray, int]]:
    """{name: (matrix, label)}; label 1 = should be flagged."""
    sets = {"normal": (_in_band(rng, n_normal), 0)}

    oor = _in_band(rng, n_each)
    cols = rng.integers(0, len(FEATURES), n_each)
    for i, c in enumerate(cols):
        low, high = VITAL_RANGES[FEATURES[c]]
        span = high - low
        push = rng.uniform(0.1, span * 0.5)
        oor[i, c] = low - push if rng.random() < 0.5 else high + push
    sets["one_vital_out_of_band"] = (oor, 1)

    dbp_over = _in_band(rng, n_each)
    sbp = rng.uniform(85, 100, n_each)
    dbp_over[:, FEATURES.index("sbp")] = sbp
    dbp_over[:, FEATURES.index("dbp")] = rng.uniform(sbp, 100)
    sets["diastolic_at_or_above_systolic"] = (dbp_over, 1)

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


def range_rule(x: np.ndarray) -> np.ndarray:
    low = np.array([VITAL_RANGES[f][0] for f in FEATURES])
    high = np.array([VITAL_RANGES[f][1] for f in FEATURES])
    return ((x < low) | (x > high)).any(axis=1)


def score(x: np.ndarray, model_path: Path = MODEL_PATH) -> tuple[np.ndarray, np.ndarray]:
    """(flagged, anomaly_score) from the ONNX model; higher score = more anomalous."""
    session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
    label, raw = session.run(None, {session.get_inputs()[0].name: x.astype(np.float32)})
    return label.ravel() == -1, -raw.ravel()


def landed_panels(landing: Path = LANDING) -> np.ndarray:
    """Real panels: per encounter and minute, the latest value of each vital;
    only minutes where all six were read."""
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


def _rate(flags: np.ndarray) -> float:
    return round(float(flags.mean()), 4) if len(flags) else 0.0


def evaluate(model_path: Path = MODEL_PATH, landing: Path | None = LANDING, seed: int = SEED) -> dict:
    rng = np.random.default_rng(seed)
    sets = test_sets(rng)
    per_set, all_labels, all_scores, all_model, all_rule = {}, [], [], [], []
    for name, (x, label) in sets.items():
        flagged, s = score(x, model_path)
        rule = range_rule(x)
        per_set[name] = {
            "n": len(x),
            "should_flag": bool(label),
            "model_flag_rate": _rate(flagged),
            "range_rule_flag_rate": _rate(rule),
        }
        all_labels.append(np.full(len(x), label))
        all_scores.append(s)
        all_model.append(flagged)
        all_rule.append(rule)

    y, s, m, r = (np.concatenate(a) for a in (all_labels, all_scores, all_model, all_rule))
    joint = np.concatenate(
        [np.full(len(x), name not in ("normal", "one_vital_out_of_band")) for name, (x, _) in sets.items()]
    )

    def prf(pred: np.ndarray, mask: np.ndarray | None = None) -> dict:
        yy, pp = (y, pred) if mask is None else (y[mask], pred[mask])
        tp, fp, fn = int(((pp) & (yy == 1)).sum()), int(((pp) & (yy == 0)).sum()), int(((~pp) & (yy == 1)).sum())
        return {
            "precision": round(tp / (tp + fp), 4) if tp + fp else None,
            "recall": round(tp / (tp + fn), 4) if tp + fn else None,
            "true_positives": tp,
            "false_positives": fp,
            "false_negatives": fn,
        }

    normal_or_joint = (y == 0) | joint
    report = {
        "model": model_path.relative_to(REPO_ROOT).as_posix()
        if model_path.is_relative_to(REPO_ROOT)
        else model_path.as_posix(),
        "seed": seed,
        "test_sets": per_set,
        "overall": {
            "model": prf(m) | {"roc_auc": round(float(roc_auc_score(y, s)), 4)},
            "range_rule": prf(r),
        },
        "joint_anomalies_only": {
            "model": prf(m, normal_or_joint)
            | {"roc_auc": round(float(roc_auc_score(y[normal_or_joint], s[normal_or_joint])), 4)},
            "range_rule": prf(r, normal_or_joint),
        },
    }

    if landing is not None and landing.exists():
        panels = landed_panels(landing)
        flagged, _ = score(panels, model_path)
        rule = range_rule(panels)
        report["landed_stream"] = {
            "panels": len(panels),
            "model_flag_rate": _rate(flagged),
            "range_rule_flag_rate": _rate(rule),
            "both": int((flagged & rule).sum()),
            "model_only": int((flagged & ~rule).sum()),
            "range_rule_only": int((~flagged & rule).sum()),
            "neither": int((~flagged & ~rule).sum()),
            "vital_correlation": np.round(np.corrcoef(panels, rowvar=False), 3).tolist(),
            "features": FEATURES,
        }
    return report


def main(argv: list[str] | None = None) -> int:
    report = evaluate()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "landed_stream"}, indent=2))
    stream = report.get("landed_stream")
    if stream:
        print(
            json.dumps({k: v for k, v in stream.items() if k not in ("vital_correlation", "features")}, indent=2)
        )
    print(f"wrote {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
