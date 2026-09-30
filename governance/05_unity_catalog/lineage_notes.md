# Lineage notes

Unity Catalog automatically captures table- and column-level lineage
(Bronze -> Silver -> Gold, plus which notebooks/pipelines produced each
table) once the DLT pipelines in `resources/dlt_pipeline.yml` have actually
run on a real workspace. It's viewed in Catalog Explorer under a table's
**Lineage** tab.

There is no live Databricks workspace in this environment, so that lineage
graph cannot be generated or screenshotted here — this note exists so a
reader isn't left assuming lineage was captured and simply not shown. Once
deployed, the expected graph is exactly the flow this repo's directory
structure encodes: `raw_patient_events` -> `bronze_patient_events` ->
`silver_contract_checked_events` -> (`silver_fct_encounters` / `silver_dim_patients` /
`silver_fct_vitals`) -> (`gold_live_vitals_by_unit`,
the Gold passthrough tables in `healthcare_agentic_de.gold`), plus the parallel, slower
`raw_provider_roster` -> ... -> `dim_providers` path.
