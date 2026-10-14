-- Unity Catalog column-mask and row-filter FUNCTIONS. Run once, before the
-- streaming pipeline builds Gold (it references them by name).
--
-- The masks and filters themselves are declared in the pipeline's Gold table
-- definitions (pipeline/04_gold/gold_encounters.py), not with ALTER TABLE
-- here: the Gold tables are pipeline-managed materialized views, and Unity
-- Catalog rejects `ALTER TABLE ... SET MASK` / `SET ROW FILTER` on them
-- ("expects a table but ... is a view" on the first real deploy).
--
-- Column masking: full_name and mrn on gold.dim_patients are redacted for
-- anyone not in the phi_unmasked group. This is the database-level half of
-- the guarantee; agent/tools/governance_guard.enforce_masking is the
-- in-process half, applied before any row reaches the orchestrator's model.

CREATE OR REPLACE FUNCTION healthcare_agentic_de.gold.mask_name_or_mrn(val STRING)
RETURN
  CASE
    WHEN is_account_group_member('phi_unmasked') THEN val
    ELSE '***REDACTED***'
  END;

-- Row filter: a clinical_reader only sees encounters for their assigned
-- unit (one account group per unit, e.g. "unit_icu", "unit_ed"), unless
-- they're also in clinical_admin. Applied to gold.fct_encounters and
-- gold.fct_encounter_history in the pipeline.
CREATE OR REPLACE FUNCTION healthcare_agentic_de.gold.unit_row_filter(unit STRING)
RETURN
  is_account_group_member('clinical_admin')
  OR is_account_group_member(CONCAT('unit_', LOWER(unit)))
  OR NOT is_account_group_member('clinical_reader');
