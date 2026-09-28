# Vertex AI (Model Registry/Endpoints) vs. MLflow for this project's ML lifecycle

`ml/train_anomaly_model.py` trains and tracks the vitals-anomaly
`IsolationForest` with a local, file-based MLflow tracking server and the
local MLflow Model Registry, then exports the trained model to ONNX for
serving via `onnxruntime` (`agent/tools/anomaly_score.py`) — no MLflow
tracking/registry server, and no Vertex AI, is needed at inference time. This
project's target cloud is GCP (see `README.md`, `databricks.yml`), where
Vertex AI's own Model Registry and Endpoints are the platform-native
alternative to MLflow's registry and to this project's own ONNX/onnxruntime
serving path. This document is an evaluation plan for that comparison, not a
completed one — Vertex AI has not been used anywhere in this project.

## Why this comparison is worth doing

This project deliberately chose the "runs anywhere, zero cloud dependency"
path for its ML lifecycle (a gitignored local `./mlruns` directory, a
committed ONNX file, `onnxruntime` for local CPU inference) specifically so
it's fully testable without live cloud credentials — see
`ml/train_anomaly_model.py`'s own docstring. Understanding what that choice
costs against a real GCP-native MLOps stack (managed registry, managed
serving endpoints, built-in monitoring/drift detection) is the honest
trade-off analysis a reviewer would want, especially since this project
otherwise targets GCP throughout (`infra/terraform/`, `databricks.yml`).

## Evaluation criteria

| Criterion | What "good" looks like | How it would be measured |
|---|---|---|
| Setup time | Time from a trained sklearn model to a servable endpoint | Wall-clock time: `mlflow.register_model` + ONNX export + `onnxruntime.InferenceSession` (this project's actual path) vs. `Vertex AI Model Registry` upload + `Endpoint.deploy()` |
| Governance/masking enforcement | N/A directly (this model never sees PHI — it scores vitals values only), but worth confirming neither path introduces an unintended data-egress path | Review whether either path would, in a real deployment, transmit raw vitals readings outside the account/project boundary at inference time |
| Answer latency | Time from a `score_vitals_anomaly` call to a result, p50/p95 | Local `onnxruntime.InferenceSession.run()` (already measured informally: sub-millisecond after the cached session is warm) vs. a real Vertex AI Endpoint's network round trip |
| Cost | $/1000 inferences, including a Vertex AI Endpoint's always-on serving cost vs. this project's zero marginal cost (runs in-process, CPU-only, no serving infrastructure) | Actual Vertex AI Endpoint billing data vs. measured local CPU time for the same inference volume |
| Ability to unit-test | Can training + inference be exercised with zero cloud credentials and zero network calls in CI, the way `tests/test_anomaly_score_tool.py` does today? | Attempt the equivalent test against a real Vertex AI Model Registry/Endpoint (almost certainly requires live GCP credentials, unlike this project's local path) |
| Vendor lock-in | Portability of the trained artifact itself | ONNX (this project's actual export format) is an open, cross-platform standard runnable via `onnxruntime` anywhere; a model registered/served purely through Vertex AI's own registry format is more GCP-specific |

## Status: Pending

This comparison has **not** been run. It requires a real, GCP-connected
project with Vertex AI enabled (Model Registry + an actual deployed
Endpoint) to compare against `ml/train_anomaly_model.py`'s local MLflow +
ONNX path — that GCP project was still being set up as of this task and was
not yet live in this environment. No scores, benchmarks, or "X is better
than Y" conclusions exist yet for any row in the table above; do not treat
this document as containing results. It will be filled in with real,
measured numbers once that project is available.
