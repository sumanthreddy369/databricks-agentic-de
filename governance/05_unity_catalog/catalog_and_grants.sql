-- Databricks-only Unity Catalog DDL. Not executed against a real workspace in
-- this environment — structurally correct SQL to run once a metastore-enabled
-- workspace exists.

CREATE CATALOG IF NOT EXISTS healthcare_agentic_de;

CREATE SCHEMA IF NOT EXISTS healthcare_agentic_de.bronze;
CREATE SCHEMA IF NOT EXISTS healthcare_agentic_de.silver;
CREATE SCHEMA IF NOT EXISTS healthcare_agentic_de.gold;

-- Service principal / role for the Kafka + Autoloader ingest jobs.
CREATE GROUP IF NOT EXISTS ingest_svc;
GRANT USE CATALOG ON CATALOG healthcare_agentic_de TO `ingest_svc`;
GRANT USE SCHEMA, CREATE TABLE, MODIFY ON SCHEMA healthcare_agentic_de.bronze TO `ingest_svc`;

-- Service principal / role for the DLT pipeline runs (Bronze -> Silver -> Gold).
CREATE GROUP IF NOT EXISTS pipeline_svc;
GRANT USE CATALOG ON CATALOG healthcare_agentic_de TO `pipeline_svc`;
GRANT USE SCHEMA, CREATE TABLE, MODIFY, SELECT ON SCHEMA healthcare_agentic_de.bronze TO `pipeline_svc`;
GRANT USE SCHEMA, CREATE TABLE, MODIFY, SELECT ON SCHEMA healthcare_agentic_de.silver TO `pipeline_svc`;
GRANT USE SCHEMA, CREATE TABLE, MODIFY, SELECT ON SCHEMA healthcare_agentic_de.gold TO `pipeline_svc`;

-- The orchestrator agent's identity (used for both DE-mode pipeline-health
-- tool calls and DA-mode query_gold_table calls). Deliberately SELECT-only on
-- Gold, and deliberately NEVER a member of `phi_unmasked` — this is the
-- database-level backstop for the same guarantee agent/tools/governance_guard.py
-- enforces in-process before any row reaches the LLM.
CREATE GROUP IF NOT EXISTS orchestrator_agent;
GRANT USE CATALOG ON CATALOG healthcare_agentic_de TO `orchestrator_agent`;
GRANT USE SCHEMA, SELECT ON SCHEMA healthcare_agentic_de.gold TO `orchestrator_agent`;
-- orchestrator_agent is intentionally NEVER granted membership in
-- `phi_unmasked` below — do not add it there.

-- Human clinical readers: SELECT on Gold, subject to the row filter in
-- row_filters_and_masking.sql restricting them to their assigned unit.
CREATE GROUP IF NOT EXISTS clinical_reader;
GRANT USE CATALOG ON CATALOG healthcare_agentic_de TO `clinical_reader`;
GRANT USE SCHEMA, SELECT ON SCHEMA healthcare_agentic_de.gold TO `clinical_reader`;

-- Break-glass group: members see unmasked PHI (full_name/mrn) and are exempt
-- from the unit-scoped row filter. Membership should be tightly controlled
-- and audited outside of this scaffold.
CREATE GROUP IF NOT EXISTS phi_unmasked;
