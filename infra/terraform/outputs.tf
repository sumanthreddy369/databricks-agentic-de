output "landing_bucket_url" {
  description = "gs:// URL for the provider-roster landing bucket — set PROVIDER_LANDING_PATH / databricks.yml's provider_landing_path variable to this value plus your sub-path (e.g. /providers)."
  value       = "gs://${google_storage_bucket.landing.name}"
}

output "databricks_service_account_email" {
  description = "Service account email to configure as the Databricks storage credential / external location identity for the landing bucket."
  value       = google_service_account.databricks_landing_access.email
}

output "pubsub_topic_id" {
  description = "Fully-qualified Pub/Sub topic id for the GCP-native Kafka alternative discussed in databricks.yml/resources/dlt_pipeline.yml."
  value       = google_pubsub_topic.patient_events.id
}

output "secret_manager_secret_ids" {
  description = "Secret Manager secret ids created as placeholders — populate actual versions out-of-band (gcloud secrets versions add), never via Terraform state."
  value = {
    anthropic_api_key   = google_secret_manager_secret.anthropic_api_key.secret_id
    langfuse_public_key = google_secret_manager_secret.langfuse_public_key.secret_id
    langfuse_secret_key = google_secret_manager_secret.langfuse_secret_key.secret_id
  }
}
