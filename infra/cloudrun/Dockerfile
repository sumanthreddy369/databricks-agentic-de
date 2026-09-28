# TIER 2 SCAFFOLD — structurally correct, NOT built or run in this
# environment (no container runtime target was exercised here beyond a
# syntax review). See deploy.sh in this directory for the `gcloud run
# deploy` command this image is meant to be used with, and that script's own
# comments for why Cloud Run is a demonstration alternative, not this
# project's actual local-dev path (docker-compose.yml's Redpanda broker).
#
# Containerizes simulator/producer.py (the patient_events publish loop) so it
# can run as a Cloud Run job/service against a real Kafka-compatible broker
# or GCP Pub/Sub (see infra/terraform/main.tf's google_pubsub_topic) reachable
# from GCP, instead of only against the local docker-compose Redpanda broker.
#
# Built on `uv` (this project's actual dependency manager — see
# pyproject.toml/uv.lock) rather than a plain pip install, so the exact
# locked dependency versions this repo tests against are what actually run.

FROM python:3.11-slim AS base

# uv's official static binary — no separate pip install of uv needed, and no
# network call to PyPI just to bootstrap the tool that installs everything
# else from PyPI.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy dependency manifests first so `uv sync` is cached across rebuilds that
# only change application code, not dependencies.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Now copy the actual source the simulator needs at runtime. Deliberately
# narrow (not `COPY . .`) — this image runs simulator/producer.py only, not
# the DE/DA agent, the Databricks-only pipeline/ code, or the test suite.
COPY common ./common
COPY simulator ./simulator
COPY agent/otel.py ./agent/otel.py
COPY agent/__init__.py ./agent/__init__.py

ENV PATH="/app/.venv/bin:$PATH"

ENTRYPOINT ["python", "-m", "simulator.producer"]
CMD ["--patients", "1000", "--duration", "60"]
