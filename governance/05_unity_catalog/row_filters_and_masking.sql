-- Databricks-only Unity Catalog column masks + row filters. Not executed
-- against a real workspace in this environment.
--
-- Column masking: full_name and mrn on dim_patients are redacted for anyone
-- not in the phi_unmasked group. This mirrors (and backstops at the SQL
-- layer) the in-process masking agent/tools/governance_guard.enforce_masking
-- performs before any row reaches the orchestrator agent's LLM calls.

CREATE OR REPLACE FUNCTION healthcare_agentic_de.gold.mask_name_or_mrn(val STRING)
RETURN
  CASE
    WHEN is_account_group_member('phi_unmasked') THEN val
    ELSE '***REDACTED***'
  END;

ALTER TABLE healthcare_agentic_de.gold.dim_patients
  ALTER COLUMN full_name SET MASK healthcare_agentic_de.gold.mask_name_or_mrn;

ALTER TABLE healthcare_agentic_de.gold.dim_patients
  ALTER COLUMN mrn SET MASK healthcare_agentic_de.gold.mask_name_or_mrn;

-- Row filter: a clinical_reader only sees encounters for their assigned
-- unit (assumed to be modeled as one account group per unit, e.g.
-- "unit_icu", "unit_ed"), unless they're also in clinical_admin.
CREATE OR REPLACE FUNCTION healthcare_agentic_de.gold.unit_row_filter(unit STRING)
RETURN
  is_account_group_member('clinical_admin')
  OR is_account_group_member(CONCAT('unit_', LOWER(unit)))
  OR NOT is_account_group_member('clinical_reader');

ALTER TABLE healthcare_agentic_de.gold.fct_encounters
  SET ROW FILTER healthcare_agentic_de.gold.unit_row_filter ON (unit);
