-- TIER 2 SCAFFOLD: illustrative Lakehouse Federation SQL, NOT run against a
-- real BigQuery dataset or a real Databricks workspace in this environment
-- (no GCP project or metastore-enabled workspace is connected here) — the
-- syntax below is structurally correct Databricks Unity Catalog Lakehouse
-- Federation DDL for a BigQuery source, not a tested/verified query result.
-- See infra/terraform/README.md and docs/comparisons/ for the same
-- "structurally correct, not executed here" distinction applied elsewhere in
-- this project.
--
-- Why this would matter for this project specifically: the provider roster
-- (pipeline/01_ingest/autoloader_provider_roster.py) is HR/credentialing
-- reference data — a real hospital IT org plausibly keeps that system of
-- record in BigQuery (e.g. alongside other GCP-native back-office systems)
-- rather than replicating it into GCS for Autoloader to pick up. Lakehouse
-- Federation lets Unity Catalog query that BigQuery table directly, with no
-- separate ETL/copy step, governed by the same Unity Catalog grants as every
-- other object in this project (see catalog_and_grants.sql).

-- Step 1: a CONNECTION is the reusable, credential-holding object — created
-- once per BigQuery project, not once per table. `google_credentials`
-- expects a service-account JSON key (or, in a real deployment, a Databricks
-- secret reference to one — never a literal key committed to a file like
-- this one).
CREATE CONNECTION IF NOT EXISTS bigquery_provider_roster_source
  TYPE bigquery
  OPTIONS (
    -- Illustrative placeholder — a real deployment would reference a
    -- Databricks secret (`secret('scope', 'key')`), never inline JSON.
    GoogleServiceAccountKeyJson secret('gcp_federation', 'bigquery_service_account_key')
  );

-- Step 2: a FOREIGN CATALOG maps one BigQuery project into Unity Catalog's
-- namespace. Every dataset/table in that BigQuery project becomes browsable
-- (subject to Unity Catalog grants, exactly like any other catalog) under
-- `bigquery_hr_systems.<bigquery_dataset>.<bigquery_table>`.
CREATE FOREIGN CATALOG IF NOT EXISTS bigquery_hr_systems
  USING CONNECTION bigquery_provider_roster_source
  OPTIONS (project 'REPLACE-WITH-YOUR-GCP-PROJECT-ID');

-- Step 3 (illustrative query, not executed here): querying the federated
-- provider roster table directly, with zero ETL/copy step, governed by
-- ordinary Unity Catalog grants (a real deployment would grant `pipeline_svc`
-- or `ingest_svc` SELECT on this foreign catalog/schema the same way
-- catalog_and_grants.sql grants access to the native `healthcare_agentic_de`
-- catalog).
--
-- SELECT * FROM bigquery_hr_systems.hr_systems.provider_roster
-- WHERE credential_status = 'active';

-- Trade-off vs. this project's actual Autoloader-based path
-- (pipeline/01_ingest/autoloader_provider_roster.py): Federation avoids the
-- landing-bucket copy step entirely, at the cost of query-time latency
-- against BigQuery (no Delta-native file pruning/caching) and a live
-- dependency on BigQuery being reachable at query time. Autoloader's
-- file-drop model was kept as this project's actual implementation because
-- the provider roster is small/slow-changing reference data where an hourly
-- batch landing in GCS (see resources/workflows.yml:reference_data_hourly)
-- is simpler to reason about and test locally (simulator/autoloader_feed.py)
-- than a live cross-cloud-service query dependency — this file exists to
-- document the alternative, not to replace that choice.
