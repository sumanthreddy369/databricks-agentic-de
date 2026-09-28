"""Tests agent/tools/anomaly_score.py against a genuinely tiny IsolationForest
trained into a tmp_path (never the full multi-thousand-row
ml/train_anomaly_model.py run, and never the committed
ml/models/vitals_anomaly.onnx artifact or the repo's real ./mlruns) — see
ml/train_anomaly_model.py's train() docstring for why its keyword args exist.
"""

import json

import pytest

from agent.tools.anomaly_score import FEATURE_ORDER, score_vitals_anomaly
from common.contracts import VITAL_ITEM_TYPES
from ml.train_anomaly_model import train

NORMAL_VITALS = {"heart_rate": 78, "spo2": 97, "resp_rate": 15, "temp_c": 37.0, "sbp": 118, "dbp": 76}


@pytest.fixture(scope="module")
def tiny_model_path(tmp_path_factory):
    tmp_dir = tmp_path_factory.mktemp("anomaly_model")
    onnx_path = tmp_dir / "tiny_vitals_anomaly.onnx"
    train(
        n_train_rows=250,
        n_holdout_rows=50,
        n_estimators=25,
        onnx_output_path=onnx_path,
        tracking_uri=f"file:{(tmp_dir / 'mlruns').as_posix()}",
    )
    return onnx_path


def test_feature_order_matches_common_contracts_vital_item_types():
    # FEATURE_ORDER is deliberately duplicated (not imported) from
    # ml/train_anomaly_model.py to keep mlflow/scikit-learn out of this
    # inference-time module's import graph — this cross-check proves the
    # duplication hasn't drifted.
    assert FEATURE_ORDER == list(VITAL_ITEM_TYPES)


def test_score_vitals_anomaly_returns_ok_shape_for_plausible_reading(tiny_model_path):
    result = score_vitals_anomaly(NORMAL_VITALS, model_path=tiny_model_path)

    assert result.is_error is False
    payload = json.loads(result.content)
    assert payload["ok"] is True
    assert payload["label"] in (1, -1)
    assert isinstance(payload["score"], float)
    assert payload["features"] == FEATURE_ORDER


def test_score_vitals_anomaly_flags_a_jointly_implausible_reading(tiny_model_path):
    # Every one of these values, taken alone, sits well outside
    # common.contracts.VITAL_RANGES too (a single-column range check would
    # already flag this one) — the point here is only that the joint model
    # is ALSO capable of flagging it, not that it's the only kind of anomaly
    # the model can catch (see this module's own docstring).
    implausible_vitals = {"heart_rate": 190, "spo2": 55, "resp_rate": 48, "temp_c": 41.5, "sbp": 230, "dbp": 20}

    result = score_vitals_anomaly(implausible_vitals, model_path=tiny_model_path)

    payload = json.loads(result.content)
    assert payload["is_anomaly"] is True
    assert payload["label"] == -1


def test_score_vitals_anomaly_rejects_missing_features(tiny_model_path):
    result = score_vitals_anomaly({"heart_rate": 80, "spo2": 97}, model_path=tiny_model_path)

    assert result.is_error is True
    payload = json.loads(result.content)
    assert payload["ok"] is False
    assert "missing required vitals" in payload["error"]


def test_score_vitals_anomaly_degrades_gracefully_when_model_file_is_missing(tmp_path):
    missing_path = tmp_path / "does_not_exist.onnx"

    result = score_vitals_anomaly(NORMAL_VITALS, model_path=missing_path)

    assert result.is_error is True
    payload = json.loads(result.content)
    assert payload["ok"] is False
    assert "not found" in payload["error"]


def test_score_vitals_anomaly_session_is_cached_across_calls(tiny_model_path, monkeypatch):
    import onnxruntime as ort

    from agent.tools import anomaly_score

    anomaly_score._load_session.cache_clear()
    real_inference_session = ort.InferenceSession
    call_count = {"n": 0}

    def counting_session(*args, **kwargs):
        call_count["n"] += 1
        return real_inference_session(*args, **kwargs)

    monkeypatch.setattr(ort, "InferenceSession", counting_session)

    score_vitals_anomaly(NORMAL_VITALS, model_path=tiny_model_path)
    score_vitals_anomaly(NORMAL_VITALS, model_path=tiny_model_path)
    score_vitals_anomaly(NORMAL_VITALS, model_path=tiny_model_path)

    assert call_count["n"] == 1  # loaded once, reused on the next two calls
    anomaly_score._load_session.cache_clear()
