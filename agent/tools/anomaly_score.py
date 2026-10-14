"""DE-mode tool: check a single multi-vital reading - rules first, model second.

Rules decide. The model only adds. Evaluated on 2026-10-14
(ml/evaluate_anomaly_model.py, docs/eda/), the committed model flagged 26% of
panels with one vital outside its alert band (the range rule: 100%) and 1-7%
of in-band joint patterns. Its training data has no correlation between
vitals to learn from (docs/eda/README.md). So `is_anomaly` is true when any
rule fires OR the model flags the reading. The rules are: a vital outside
VITAL_RANGES, a physically impossible value (PHYSIOLOGIC_LIMITS), and
diastolic at or above systolic. `decided_by` says which fired, and the model
is reported as advisory until a retrained one beats the rules.

Original rationale, kept for the retrained model:
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


@functools.lru_cache(maxsize=8)
def _load_session(model_path: str) -> ort.InferenceSession:
    """Cached by resolved model path so repeated calls (the normal case —
    one orchestrator process scoring many readings) never reload/reparse the
    ONNX graph after the first call. `maxsize=8` is generous headroom for
    tests that point at several different tmp_path fixture models in the
    same process; production only ever exercises one path.
    """
    return ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])


MODEL_STATUS = (
    "advisory - in evaluation (docs/eda/model_eval.json) it caught 26% of panels with one vital out of band "
    "and 1-7% of in-band joint patterns; the rules decide"
)


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


def score_vitals_anomaly(vitals: dict, *, model_path: Path | str | None = None) -> ToolResult:
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
    model_flag = label == -1
    decided_by = "+".join(name for name, fired in (("rules", findings), ("model", model_flag)) if fired) or "none"

    payload = {
        "ok": True,
        "is_anomaly": bool(findings) or model_flag,
        "decided_by": decided_by,
        "rule_findings": findings,
        "model_is_anomaly": model_flag,
        "model_status": MODEL_STATUS,
        "label": label,
        "score": score,
        "features": FEATURE_ORDER,
    }
    return ToolResult(tool_use_id="", content=json.dumps(payload))
