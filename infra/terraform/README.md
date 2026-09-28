# Terraform: GCP resources Databricks Asset Bundles don't cover

**This has NOT been applied anywhere.** `terraform init`, `plan`, and `apply`
have not been run against this configuration in this environment — there is
no GCP project or set of GCP credentials connected here. Everything in this
directory is structurally correct Terraform HCL, reviewed for syntax and
internal consistency, but genuinely untested against a real GCP API. Treat it
the same way this project treats `pipeline/`, `governance/05_unity_catalog/`,
and `databricks.yml`/`resources/*.yml`: correct-looking infrastructure code
that has never actually been provisioned, not a claim that it works.

## Division of responsibility vs. Databricks Asset Bundles

`databricks.yml` + `resources/*.yml` (the Databricks Asset Bundle) manage
**Databricks-side** objects: the DLT pipeline definition, the job/workflow
schedule, workspace-level configuration. This Terraform configuration manages
the **underlying GCP infrastructure** those Databricks-side jobs read from,
write to, and authenticate against — a GCS bucket, a service account and IAM
binding, a Pub/Sub topic, and Secret Manager secret containers. Neither tool
manages the other's layer; a real deployment applies this Terraform first
(so the bucket/service-account/topic exist), then runs `databricks bundle
deploy` against them.

## What's here

- `main.tf` — a GCS bucket (the provider-roster Autoloader landing path +
  DLT storage), a GCP service account scoped via IAM binding to exactly that
  bucket (principle of least privilege, matching
  `governance/05_unity_catalog/access_policy_notes.md`'s philosophy for the
  `orchestrator_agent` Unity Catalog grant), a Pub/Sub topic (the GCP-native
  alternative to self-hosted Kafka/Redpanda already noted inline in
  `databricks.yml` and `resources/dlt_pipeline.yml`), and three Secret
  Manager secret **containers** (`ANTHROPIC_API_KEY`, the two Langfuse keys)
  with no secret *versions* defined — populating real key material is left
  as a manual/CI step, never something Terraform state or this repo holds.
- `variables.tf` — every value above is parameterized; `gcp_project_id` has
  no default and must be supplied.
- `outputs.tf` — the bucket URL, service account email, Pub/Sub topic id,
  and secret ids, for wiring into `databricks.yml`'s variables and
  `.env`/Databricks secret scopes.

## What you would actually run, against your own GCP project

```bash
cd infra/terraform
terraform init
terraform plan -var="gcp_project_id=your-real-project-id"
# review the plan output carefully, then:
terraform apply -var="gcp_project_id=your-real-project-id"
```

After a real `apply`, populate the Secret Manager secret versions
out-of-band (never via Terraform, never committed to this repo):

```bash
echo -n "sk-ant-..." | gcloud secrets versions add anthropic-api-key --data-file=-
```

Then point `databricks.yml`'s `provider_landing_path` variable (and
`PROVIDER_LANDING_PATH` in `.env`) at this configuration's
`landing_bucket_url` output, and set `GCP_PROJECT_ID` so
`agent/secrets.py:get_secret()` starts resolving secrets from Secret Manager
instead of falling back to plain environment variables.
