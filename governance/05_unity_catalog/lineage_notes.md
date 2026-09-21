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
(`fct_encounters` / `dim_patients` / `fct_vitals`) -> (`gold_live_vitals_by_unit`,
the Gold passthrough tables), plus the parallel, slower
`raw_provider_roster` -> ... -> `dim_providers` path.
