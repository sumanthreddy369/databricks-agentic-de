"""Trains a real, joint multi-vital anomaly detector and exports it to ONNX.

`common.contracts.VITAL_RANGES` already gives per-vital, single-value range
checks (used by `simulator/domain.py` and `pipeline/03_silver`'s DLT
expectations). This module goes a step further: an unsupervised
`sklearn.ensemble.IsolationForest` learns what a *jointly plausible* panel of
vitals looks like (heart_rate, spo2, resp_rate, temp_c, sbp, dbp together),
so it can flag a reading where every individual value is in-range but the
*combination* is not (e.g. a normal heart rate with a dangerously low SpO2 and
a high respiratory rate at the same time) — something no single-column range
check can ever catch.

Training data is produced by calling `simulator/domain.py`'s own
`generate_vitals_event` once per vital type per synthetic "reading" (the same
generator the rest of this repo uses for the Kafka stream), not a separately
invented data source — see that module's docstring for the dataset-sourcing
rationale this project follows throughout.

Runnable via:

    uv run python -m ml.train_anomaly_model

Idempotent: re-running retrains a fresh IsolationForest on freshly generated
synthetic data, re-exports ml/models/vitals_anomaly.onnx (overwriting it),
and registers a new version of `vitals_anomaly_detector` in the local MLflow
Model Registry — nothing about a re-run conflicts with a previous run.

MLflow tracking is a local, file-based `./mlruns` directory (gitignored) —
this is a genuine, real MLflow run (params/metrics/model all actually
logged), just pointed at a local file store instead of a remote tracking
server, so it works with zero external services or credentials.
"""

import json
import os
import random
from pathlib import Path

# MLflow 3.x puts the plain filesystem tracking/registry backend ("./mlruns")
# into "maintenance mode" and refuses to use it unless this is set, nudging
# users toward a database-backed store. We deliberately keep the plain
# file-based backend (this is exactly the zero-external-server,
# zero-credentials backend this project needs — no sqlite file, no server
# process, nothing beyond a gitignored local directory) rather than adding a
# SQLite dependency for what a portfolio/demo project doesn't need. This must
# be set before any `mlflow.*` call that touches the tracking/registry store.
os.environ.setdefault("MLFLOW_ALLOW_FILE_STORE", "true")

import mlflow  # noqa: E402
import mlflow.sklearn  # noqa: E402
import numpy as np  # noqa: E402
import sklearn  # noqa: E402
from skl2onnx import convert_sklearn  # noqa: E402
from skl2onnx.common.data_types import FloatTensorType  # noqa: E402
from sklearn.ensemble import IsolationForest  # noqa: E402

from common.contracts import VITAL_ITEM_TYPES  # noqa: E402
from simulator.domain import generate_vitals_event  # noqa: E402

# Fixed column order used both when building the training matrix here and
# when agent/tools/anomaly_score.py builds a single-row inference input —
# the ONNX model has no column names of its own, so this list IS the schema.
FEATURE_ORDER: list[str] = list(VITAL_ITEM_TYPES)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ONNX_OUTPUT_PATH = REPO_ROOT / "ml" / "models" / "vitals_anomaly.onnx"
DEFAULT_MLFLOW_TRACKING_URI = f"file:{(REPO_ROOT / 'mlruns').as_posix()}"

MLFLOW_EXPERIMENT_NAME = "vitals-anomaly-detection"
REGISTERED_MODEL_NAME = "vitals_anomaly_detector"

DEFAULT_N_TRAIN_ROWS = 4000
DEFAULT_N_HOLDOUT_ROWS = 500
DEFAULT_CONTAMINATION = 0.05
# 100 trees keeps the committed ONNX artifact well under 1MB while still
# giving IsolationForest's ensemble-averaged anomaly score plenty of trees to
# average over (this is a synthetic 6-feature panel, not a high-dimensional
# problem that would need many more).
DEFAULT_N_ESTIMATORS = 100
_SYNTHETIC_PATIENT_ID = "synthetic-training-patient"
_SYNTHETIC_ENCOUNTER_ID = "synthetic-training-encounter"


def _generate_feature_matrix(n_rows: int, *, seed: int) -> np.ndarray:
    """Builds an (n_rows, len(FEATURE_ORDER)) float32 matrix by calling
    `simulator.domain.generate_vitals_event` once per feature per row — the
    same generator (including its `OUT_OF_RANGE_PROBABILITY` tail) that
    produces the live Kafka stream, just called directly rather than
    replayed from a broker.
    """
    rng_state = random.getstate()
    random.seed(seed)
    try:
        rows = []
        for _ in range(n_rows):
            row = [
                generate_vitals_event(_SYNTHETIC_PATIENT_ID, _SYNTHETIC_ENCOUNTER_ID, itemid).vital.value
                for itemid in FEATURE_ORDER
            ]
            rows.append(row)
    finally:
        random.setstate(rng_state)
    return np.array(rows, dtype=np.float32)


def train(
    *,
    n_train_rows: int = DEFAULT_N_TRAIN_ROWS,
    n_holdout_rows: int = DEFAULT_N_HOLDOUT_ROWS,
    contamination: float = DEFAULT_CONTAMINATION,
    n_estimators: int = DEFAULT_N_ESTIMATORS,
    onnx_output_path: Path | str | None = None,
    tracking_uri: str | None = None,
    random_state: int = 13,
) -> dict:
    """Trains, evaluates, exports, and registers the model. Returns a small
    dict summary (also what `main()` prints) so both the CLI and tests can
    inspect the outcome without re-parsing stdout.

    Parameters are keyword-only and all have defaults sized for a real
    training run; tests pass small `n_train_rows`/`n_holdout_rows` (a few
    hundred rows is enough for a test-scale IsolationForest) plus a
    `tmp_path`-scoped `onnx_output_path`/`tracking_uri` so the test suite
    never touches the committed `ml/models/vitals_anomaly.onnx` or the repo's
    real `./mlruns` directory.
    """
    mlflow.set_tracking_uri(tracking_uri or DEFAULT_MLFLOW_TRACKING_URI)
    mlflow.set_experiment(MLFLOW_EXPERIMENT_NAME)

    x_train = _generate_feature_matrix(n_train_rows, seed=random_state)
    x_holdout = _generate_feature_matrix(n_holdout_rows, seed=random_state + 1)

    model = IsolationForest(contamination=contamination, n_estimators=n_estimators, random_state=random_state)
    model.fit(x_train)

    holdout_predictions = model.predict(x_holdout)  # -1 = anomaly, 1 = normal
    fraction_flagged_anomalous = float(np.mean(holdout_predictions == -1))

    onnx_path = Path(onnx_output_path) if onnx_output_path else DEFAULT_ONNX_OUTPUT_PATH
    onnx_path.parent.mkdir(parents=True, exist_ok=True)
    onnx_model = convert_sklearn(
        model,
        initial_types=[("vitals_panel", FloatTensorType([None, len(FEATURE_ORDER)]))],
        # skl2onnx 1.20's IsolationForest converter defaults to ai.onnx.ml
        # opset 4, which the same library's own ONNX-writing step doesn't yet
        # recognize as a valid target ("not supported yet by this library").
        # Pinning both domains explicitly (the ai.onnx.ml pin alone still
        # left the main '' domain unresolved and hit a second internal
        # skl2onnx bug) avoids that mismatch — purely an ONNX opset pin,
        # unrelated to the scikit-learn model itself.
        target_opset={"": 15, "ai.onnx.ml": 3},
    )
    onnx_path.write_bytes(onnx_model.SerializeToString())

    with mlflow.start_run(run_name="isolation_forest_vitals_anomaly") as run:
        mlflow.log_param("contamination", contamination)
        mlflow.log_param("n_estimators", n_estimators)
        mlflow.log_param("n_train_rows", n_train_rows)
        mlflow.log_param("n_holdout_rows", n_holdout_rows)
        mlflow.log_param("feature_order", ",".join(FEATURE_ORDER))
        mlflow.log_metric("fraction_flagged_anomalous_holdout", fraction_flagged_anomalous)

        model_info = mlflow.sklearn.log_model(
            model,
            artifact_path="model",
            # mlflow's sklearn flavor serializes via skops (safer than raw
            # pickle) and by default refuses to trust `sklearn.tree._tree.
            # Tree` (the shared node storage IsolationForest's internal trees
            # use) because a *hostile* file could abuse it. This model was
            # just trained in-process above, not loaded from an untrusted
            # source, so trusting this one type for our own artifact is safe.
            skops_trusted_types=["sklearn.tree._tree.Tree"],
            # Without an explicit pip_requirements list, mlflow shells out to
            # `uv export` to capture a full environment snapshot for the
            # logged model — correct behavior for a model someone else will
            # `mlflow models serve`, but ~40s of pure overhead for a portfolio
            # project's local runs and tests, where the ONNX export below
            # (not this sklearn-flavor artifact) is the thing actually served.
            pip_requirements=[f"scikit-learn=={sklearn.__version__}"],
        )
        mlflow.log_artifact(str(onnx_path), artifact_path="onnx")

        registered_version = mlflow.register_model(model_info.model_uri, REGISTERED_MODEL_NAME)

        run_id = run.info.run_id

    return {
        "run_id": run_id,
        "registered_model_name": REGISTERED_MODEL_NAME,
        "registered_model_version": registered_version.version,
        "fraction_flagged_anomalous_holdout": fraction_flagged_anomalous,
        "onnx_path": str(onnx_path),
        "tracking_uri": mlflow.get_tracking_uri(),
    }


def main() -> None:
    summary = train()
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
