"""DE-mode tool: check a single multi-vital reading with three layers, each
reported separately in `decided_by`:

1. rules a person can check by eye: a vital outside VITAL_RANGES, a
   physically impossible value (PHYSIOLOGIC_LIMITS), diastolic >= systolic;
2. NEWS2, the published early-warning score (agent/tools/early_warning.py);
3. the anomaly model - only at the threshold its evaluation justified.

`is_anomaly` is true if any layer fires. History (docs/eda/): model v1,
trained on uniform independent vitals, caught 26% of single out-of-band
vitals and 1-7% of joint patterns, so it was demoted to advisory. Model v2,
trained on stable patients from simulator/physiology.py, catches 61% of
deterioration at NEWS2's false-alarm rate (NEWS2: 47%), notably hypertensive
crisis, which NEWS2 can't see. ml/evaluate_anomaly_model.py writes that
threshold to ml/models/vitals_anomaly_calibration.json; without it, or for a
model it wasn't computed for, the model's own label is used and reported as
advisory. Validated on simulated physiology only - never on real patients.

Original rationale for the model:
`common.contracts.VITAL_RANGES` (used by `simulator/domain.py` and the
Silver-layer DLT expectations) is a per-column range check: it can only ever
flag one out-of-band value at a time. This tool instead scores the six vitals
*together*, via the `IsolationForest` model `ml/train_anomaly_model.py`
trains and exports to ONNX — it can flag a reading where every individual
value is technically in-range but the combination is not (e.g. a normal heart
rate with a critically low SpO2 and an elevated respiratory rate at the same
time).

Inference runs entirely locally via `onnxruntime` against the committed
`ml/models/vitals_anomaly.onnx` artifact — no network call, no Databricks
workspace, no MLflow server needed at inference time (MLflow is only used at
*training* time, in `ml/train_anomaly_model.py`).
"""

import functools
import json
from pathlib import Path

import numpy as np
import onnxruntime as ort

from agent.llm import ToolResult
from agent.tools.early_warning import news2
from common.contracts import PHYSIOLOGIC_LIMITS, VITAL_ITEM_TYPES, VITAL_RANGES

# Deliberately duplicated (not imported) from ml/train_anomaly_model.py:
# that module pulls in mlflow/scikit-learn/skl2onnx for TRAINING, multi-
# second imports this INFERENCE-time module must never pay just to read two
# constants — that's the whole point of exporting to ONNX in the first
# place. Both modules derive FEATURE_ORDER from the same
# common.contracts.VITAL_ITEM_TYPES, so they cannot silently drift apart;
# `tests/test_anomaly_score_tool.py` cross-checks this explicitly.
FEATURE_ORDER: list[str] = list(VITAL_ITEM_TYPES)
DEFAULT_ONNX_MODEL_PATH = Path(__file__).resolve().parent.parent.parent / "ml" / "models" / "vitals_anomaly.onnx"
DEFAULT_CALIBRATION_PATH = DEFAULT_ONNX_MODEL_PATH.with_name("vitals_anomaly_calibration.json")


@functools.lru_cache(maxsize=8)
def _load_session(model_path: str) -> ort.InferenceSession:
    """Cached by resolved model path so repeated calls (the normal case —
    one orchestrator process scoring many readings) never reload/reparse the
    ONNX graph after the first call. `maxsize=8` is generous headroom for
    tests that point at several different tmp_path fixture models in the
    same process; production only ever exercises one path.
    """
    return ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])


ADVISORY_STATUS = (
    "advisory - no evaluated threshold for this model; its own label is shown, rules and NEWS2 decide"
)


def _calibration(path: Path) -> dict | None:
    try:
        calibration = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return calibration if calibration.get("promoted") else None


def rule_findings(vitals: dict) -> list[str]:
    """Deterministic checks a person can verify by eye."""
    findings = []
    for name in FEATURE_ORDER:
        value = float(vitals[name])
        plo, phi = PHYSIOLOGIC_LIMITS[name]
        lo, hi = VITAL_RANGES[name]
        if not plo <= value <= phi:
            findings.append(f"{name}={value:g} is physically impossible (possible: {plo}-{phi})")
        elif not lo <= value <= hi:
            side = "below" if value < lo else "above"
            findings.append(f"{name}={value:g} is {side} the alert band {lo}-{hi}")
    if float(vitals["dbp"]) >= float(vitals["sbp"]):
        findings.append(
            f"dbp={float(vitals['dbp']):g} is not below sbp={float(vitals['sbp']):g} (impossible pair)"
        )
    return findings


def score_vitals_anomaly(
    vitals: dict, *, model_path: Path | str | None = None, calibration_path: Path | str | None = None
) -> ToolResult:
    """Scores one multi-vital reading (a dict keyed by the same vital item
    ids as `common.contracts.VITAL_ITEM_TYPES` — heart_rate, spo2, resp_rate,
    temp_c, sbp, dbp) for joint-anomaly risk.

    `model_path` defaults to the committed `ml/models/vitals_anomaly.onnx`
    artifact; tests override it with a tiny model trained into a `tmp_path`
    (see `ml.train_anomaly_model.train`) so this is fully testable without
    depending on the full multi-thousand-row training run.

    Returns a ToolResult whose content is a JSON object:
    `{"ok": true, "is_anomaly": bool, "label": 1|-1, "score": float,
    "features": [...]}`, or an error ToolResult (never a crash) if the model
    file is missing or `vitals` is missing a required feature.
    """
    resolved_path = Path(model_path) if model_path is not None else DEFAULT_ONNX_MODEL_PATH
    if not resolved_path.exists():
        return ToolResult(
            tool_use_id="",
            content=json.dumps(
                {
                    "ok": False,
                    "error": (
                        f"anomaly model not found at {resolved_path}; run "
                        "`uv run python -m ml.train_anomaly_model` first"
                    ),
                }
            ),
            is_error=True,
        )

    missing = [f for f in FEATURE_ORDER if f not in vitals]
    if missing:
        return ToolResult(
            tool_use_id="",
            content=json.dumps({"ok": False, "error": f"missing required vitals: {missing}"}),
            is_error=True,
        )

    session = _load_session(str(resolved_path))
    features = np.array([[float(vitals[name]) for name in FEATURE_ORDER]], dtype=np.float32)
    input_name = session.get_inputs()[0].name
    outputs = session.run(None, {input_name: features})

    # skl2onnx's IsolationForest converter emits two outputs, in this order:
    # "label" (1 = normal, -1 = anomaly) and "scores" (the raw decision-
    # function-shaped anomaly score; lower/more negative = more anomalous).
    label = int(outputs[0][0][0])
    score = float(outputs[1][0][0])
    findings = rule_findings(vitals)
    warning = news2(vitals)

    # The evaluated threshold belongs to the committed model only.
    if calibration_path is None and model_path is None:
        calibration_path = DEFAULT_CALIBRATION_PATH
    calibration = _calibration(Path(calibration_path)) if calibration_path else None
    if calibration:
        model_flag = -score > calibration["score_threshold"]
        model_status = (
            f"calibrated - threshold matched to NEWS2's false-alarm rate on stable patients "
            f"({calibration['stable_false_positive_rate']:.1%}); {calibration['caveat']}"
        )
    else:
        model_flag = label == -1
        model_status = ADVISORY_STATUS

    fired = (("rules", bool(findings)), ("news2", warning["trigger"]), ("model", model_flag))
    decided_by = "+".join(name for name, hit in fired if hit) or "none"
    payload = {
        "ok": True,
        "is_anomaly": decided_by != "none",
        "decided_by": decided_by,
        "rule_findings": findings,
        "news2": {k: warning[k] for k in ("score", "risk", "trigger", "single_parameter_3")},
        "model_is_anomaly": model_flag,
        "model_status": model_status,
        "label": label,
        "score": score,
        "features": FEATURE_ORDER,
    }
    return ToolResult(tool_use_id="", content=json.dumps(payload))
