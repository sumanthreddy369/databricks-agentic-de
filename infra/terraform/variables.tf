variable "gcp_project_id" {
  description = "GCP project id these resources are created in. No default — must be supplied by the caller (terraform.tfvars, -var, or TF_VAR_gcp_project_id)."
  type        = string
}

variable "gcp_region" {
  description = "GCP region for regional resources (the GCS bucket location below uses a multi-region-style default instead — see bucket_location)."
  type        = string
  default     = "us-central1"
}

variable "bucket_location" {
  description = "GCS bucket location for the provider-roster landing bucket. A multi-region value (e.g. US) is simplest for a demo; a real deployment would likely pin this to gcp_region instead."
  type        = string
  default     = "US"
}

variable "landing_bucket_name" {
  description = "Name of the GCS bucket Autoloader watches for provider-roster batch files and DLT uses for pipeline storage. Must be globally unique across all of GCS."
  type        = string
  default     = "healthcare-agentic-de-landing"
}

variable "databricks_service_account_id" {
  description = "Account id (the part before @<project>.iam.gserviceaccount.com) for the service account Databricks uses to read/write the landing bucket."
  type        = string
  default     = "databricks-agentic-de-landing"
}

variable "pubsub_topic_name" {
  description = "Name of the Pub/Sub topic standing in for the GCP-native alternative to self-hosted Kafka/Redpanda for the continuous patient_events ingestion path (see databricks.yml and resources/dlt_pipeline.yml's inline notes on this trade-off — Kafka/Redpanda is what this project actually runs against, for local-dev parity)."
  type        = string
  default     = "patient-events"
}

variable "anthropic_api_key_secret_id" {
  description = "Secret Manager secret id for ANTHROPIC_API_KEY. This resource only creates the secret CONTAINER — populating an actual secret VERSION with a real key is deliberately left as a manual/CI step, never something Terraform state or this repo should hold in plaintext."
  type        = string
  default     = "anthropic-api-key"
}

variable "langfuse_public_key_secret_id" {
  description = "Secret Manager secret id for LANGFUSE_PUBLIC_KEY."
  type        = string
  default     = "langfuse-public-key"
}

variable "langfuse_secret_key_secret_id" {
  description = "Secret Manager secret id for LANGFUSE_SECRET_KEY."
  type        = string
  default     = "langfuse-secret-key"
}
