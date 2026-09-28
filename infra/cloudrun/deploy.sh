#!/usr/bin/env bash
# TIER 2 SCAFFOLD — NOT executed in this environment (no GCP project/gcloud
# auth connected here). Structurally correct `gcloud run deploy` invocation
# for the Dockerfile in this same directory; read every comment before
# actually running this against a real project.
#
# Why this exists as a documented alternative, not this project's real path:
# this project's actual local-dev/test path for the simulator is
# `docker-compose.yml`'s Redpanda broker + `python -m simulator.producer`
# run directly (see README.md's "Setup order"). Cloud Run is useful for
# demonstrating "the simulator can run as a managed, scale-to-zero container
# against a real cloud broker" without standing up a persistent VM — it is
# NOT a replacement for the Kafka-vs-Autoloader-vs-Pub/Sub trade-off already
# discussed in databricks.yml/resources/dlt_pipeline.yml, and it has never
# actually been deployed for this project.

set -euo pipefail

# --- required: edit these before running -----------------------------------
PROJECT_ID="${PROJECT_ID:?set PROJECT_ID to your real GCP project id}"
REGION="${REGION:-us-central1}"
SERVICE_NAME="${SERVICE_NAME:-databricks-agentic-de-simulator}"
REPOSITORY="${REPOSITORY:-databricks-agentic-de}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
IMAGE_URI="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/simulator:${IMAGE_TAG}"

# 1. Build and push the image via Cloud Build (avoids needing a local Docker
#    daemon / cross-compiling for Cloud Run's linux/amd64 target).
gcloud builds submit \
  --project="${PROJECT_ID}" \
  --tag="${IMAGE_URI}" \
  --file=infra/cloudrun/Dockerfile \
  .

# 2. Deploy as a Cloud Run job (not a long-lived Service) — the simulator's
#    `--duration` flag already makes each run a bounded, finite task, which
#    is exactly the "run to completion" shape a Cloud Run Job (not Service)
#    is for; a Service would keep an HTTP listener alive that this CLI-only
#    producer doesn't have.
gcloud run jobs deploy "${SERVICE_NAME}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --image="${IMAGE_URI}" \
  --args="--patients=1000,--duration=300,--bootstrap-servers=${KAFKA_BOOTSTRAP_SERVERS:?set KAFKA_BOOTSTRAP_SERVERS to a broker reachable from this Cloud Run job}" \
  --max-retries=1 \
  --task-timeout=600 \
  --cpu=1 \
  --memory=512Mi

# 3. Execute it (Cloud Run Jobs are deploy-then-execute, not deploy-and-run):
#
#   gcloud run jobs execute "${SERVICE_NAME}" --project="${PROJECT_ID}" --region="${REGION}"
#
# left as a separate, explicit step rather than chained here — a real
# deployment would likely trigger this from a scheduler (Cloud Scheduler) or
# CI pipeline, not run it inline with the deploy step.
