# TIER 2 SCAFFOLD — structurally correct Terraform, NOT applied (never
# `terraform init`/`plan`/`apply`'d) in this environment: no GCP project or
# credentials exist here. See this directory's README.md for what to run
# against a real project. This covers exactly the GCP resources Databricks
# Asset Bundles (databricks.yml, resources/*.yml) don't: the bundle manages
# Databricks-side objects (the DLT pipeline, jobs, workspace config); this
# file manages the underlying GCP infrastructure those Databricks-side jobs
# read from/write to and authenticate against.

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

provider "google" {
  project = var.gcp_project_id
  region  = var.gcp_region
}

# --- GCS bucket: provider-roster Autoloader landing path + DLT storage -----
# Backs `PROVIDER_LANDING_PATH` (pipeline/01_ingest/autoloader_provider_roster.py's
# `cloudFiles` source, default gs://healthcare-agentic-de-landing/providers)
# and doubles as the DLT pipeline's storage location
# (resources/dlt_pipeline.yml).

resource "google_storage_bucket" "landing" {
  name     = var.landing_bucket_name
  location = var.bucket_location

  # Uniform bucket-level IAM only (no legacy per-object ACLs) — simpler to
  # reason about alongside the IAM binding below, and matches this project's
  # minimum-necessary-access philosophy (see
  # governance/05_unity_catalog/access_policy_notes.md for the same
  # philosophy applied to Unity Catalog grants).
  uniform_bucket_level_access = true

  # Provider-roster batch files and DLT checkpoint/storage data are
  # regenerable from source systems (the HR/credentialing system of record,
  # and the DLT pipeline's own re-run), not the durable system of record
  # themselves — versioning is left off rather than adding storage cost for
  # data this project doesn't treat as needing point-in-time recovery.
  versioning {
    enabled = false
  }

  force_destroy = false
}

# --- GCP service account: scoped to exactly this bucket ---------------------
# The identity Databricks' Autoloader/DLT jobs authenticate as when reading
# the landing bucket. Principle of least privilege, matching
# governance/05_unity_catalog/access_policy_notes.md's philosophy for
# `orchestrator_agent`'s Unity Catalog grant: this service account can read
# and write objects in `google_storage_bucket.landing` and NOTHING else in
# the GCP project — no project-wide Storage Admin role, no access to any
# other bucket.

resource "google_service_account" "databricks_landing_access" {
  account_id   = var.databricks_service_account_id
  display_name = "Databricks Autoloader/DLT access to the provider-roster landing bucket"
}

resource "google_storage_bucket_iam_member" "databricks_landing_object_admin" {
  bucket = google_storage_bucket.landing.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.databricks_landing_access.email}"
}

# --- Pub/Sub topic: GCP-native alternative to self-hosted Kafka -----------
# Stands in for the "GCP Pub/Sub is the cloud-native managed alternative to
# self-hosting Kafka/Redpanda for this same continuous ingestion path" trade-
# off already noted inline in databricks.yml and resources/dlt_pipeline.yml.
# This project's ACTUAL patient_events ingestion runs against Kafka/Redpanda
# (docker-compose.yml) for local-dev parity between the simulator and any
# real deployment target — this topic is provisioned for completeness/
# comparison, not swapped in as the pipeline's real ingestion path.

resource "google_pubsub_topic" "patient_events" {
  name = var.pubsub_topic_name
}

# --- Secret Manager: placeholders for credentials the agent reads via ------
# agent/secrets.py:get_secret() when GCP_PROJECT_ID is configured. These
# resources create the secret CONTAINERS only — no secret_version resource
# is defined here, deliberately: populating the actual key material is left
# as a manual (or separate, access-controlled CI) step, never something
# committed to this repo or held in Terraform state.

resource "google_secret_manager_secret" "anthropic_api_key" {
  secret_id = var.anthropic_api_key_secret_id

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "langfuse_public_key" {
  secret_id = var.langfuse_public_key_secret_id

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "langfuse_secret_key" {
  secret_id = var.langfuse_secret_key_secret_id

  replication {
    auto {}
  }
}
