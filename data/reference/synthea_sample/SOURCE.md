# Synthea sample dataset (reference artifact)

- **File**: `synthea_sample_data_csv_latest.zip` (5.7 MB compressed, ~62 MB uncompressed CSVs)
- **Source**: https://synthea.mitre.org/downloads → "100 Sample Synthetic Patient Records, CSV"
- **Direct URL**: https://synthetichealth.github.io/synthea-sample-data/downloads/latest/synthea_sample_data_csv_latest.zip
- **Downloaded**: 2026-09-21
- **License**: Free from cost, privacy, and security restrictions; usable without restriction
  for academic, research, industry, and government secondary uses, per MITRE's Synthea
  downloads page. No real patient data — Synthea generates fully synthetic patient records.
- **Citation**:
  > Jason Walonoski, Mark Kramer, Joseph Nichols, Andre Quina, Chris Moesel, Dylan Hall,
  > Carlton Duffett, Kudakwashe Dube, Thomas Gallagher, Scott McLachlan, *Synthea: An approach,
  > method, and software mechanism for generating synthetic patients and the synthetic
  > electronic health care record*, Journal of the American Medical Informatics Association,
  > Volume 25, Issue 3, March 2018, Pages 230-238, https://doi.org/10.1093/jamia/ocx079

## What's inside

100 synthetic patients' full records across 18 CSVs: `patients`, `encounters`, `conditions`,
`observations`, `medications`, `procedures`, `immunizations`, `allergies`, `careplans`,
`devices`, `imaging_studies`, `supplies`, `providers`, `organizations`, `payers`,
`payer_transitions`, `claims`, `claims_transactions`.

## Role in this project

This is a **documented reference artifact only** — nothing in `pipeline/`, `agent/`, or
`simulator/` reads this zip at runtime. It's included so the entity shapes and field
conventions this project's own generator (`simulator/domain.py`) is modeled on are auditable
and cited, not just asserted. To actually build a higher-fidelity version of this pipeline
against real Synthea output, unzip this file and point a loader at `patients.csv` /
`encounters.csv` / `observations.csv` in place of `simulator/domain.py`'s generator functions —
`simulator/population.py`, `simulator/producer.py`, and the entire downstream pipeline are
unchanged, since they only depend on the `PatientEvent`/`common.contracts` shape, not on how
the data was produced.
