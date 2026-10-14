# EDA of the landed stream, and a real test of the anomaly model

Date: 2026-10-14. Data: the 103,301 events in `data/landing/`, the same files
the Databricks pipeline ingested. Everything here runs locally (DuckDB over
the raw JSONL), costs nothing, and is reproducible:

```bash
uv run python -m ml.evaluate_anomaly_model                 # docs/eda/model_eval.json
uv run --with matplotlib python scripts/eda_landing.py     # docs/eda/eda_summary.json + charts
```

Every number below is in `eda_summary.json` or `model_eval.json`.

## Short version

- **The pipeline's input is clean.** No duplicates, missing keys, late or
  out-of-order events, or broken encounter lifecycles. Silver's 99,213
  vitals rows are exactly 99,536 readings minus 323 physically impossible
  ones.
- **One real data-quality defect:** all 13 readmitted patients came back
  with a different name and birth date under the same `patient_id`.
- **The vitals aren't physiological.** Each one is uniform across its alert
  band and independent of every other vital, the patient and the unit.
- **So the anomaly model can't work, and it doesn't.** It catches 26% of
  panels with one vital out of band (the existing range rule catches 100%),
  and 1-7% of the joint patterns it was built for.
- **Round 1 fix:** the agent's anomaly tool let deterministic rules decide
  and made the model advisory.
- **Round 2:** the simulator now has physiology (`simulator/physiology.py`).
  Retrained on it, model v2 catches 61% of deteriorating patients at
  NEWS2's false-alarm rate (NEWS2: 47%), including hypertensive crisis,
  which NEWS2 can't see. It is now a calibrated third layer after the rules
  and NEWS2. Validated on simulated physiology only.

## The data

| | |
|---|---|
| Events | 103,301 (99,536 vitals, 1,401 admissions, 1,201 discharges, 1,163 transfers) |
| Patients / encounters | 1,388 / 1,401 (200 still open) |
| Files | 10 patient-event files, 3 provider files (30 providers) |
| Event time | 2026-10-07 02:18:34 to 02:28:34: ten minutes of simulated time |
| Schema versions | 1 |
| Readings per encounter | median 54, max 529 |
| Median length of stay | 61 seconds (the simulator compresses time) |

Admissions are spread almost evenly across units (Cardiology 253, ED 285,
ICU 272, MedSurg 287, Oncology 304), encounter types, genders, regions and
age bands. 74 patients are under 10, and they get the same adult vital
ranges as everyone else.

## Data-quality checks

| Check | Result |
|---|---|
| Duplicate event ids / duplicate contents | 0 / 0 |
| Missing patient, encounter or timestamp | 0 |
| Late or out-of-order events within a file | 0 |
| Encounters without an admission | 0 |
| Encounters admitted or discharged twice | 0 |
| Discharged before admitted | 0 |
| Vitals before admission / after discharge | 0 / 0 |
| Transfers to the same unit | 0 |
| Vital readings outside the alert band | 3,083 (about 3% per vital, by design) |
| Physically impossible readings | 323: 300 SpO2 above 100%, 23 respiratory rates of 0 |
| **Readmitted patients whose identity changed** | **13 of 13: new name and birth date; 8 new gender, 11 new region** |

The impossible readings are exactly what Silver's `plausible_vital_value`
expectation drops: 99,536 - 323 = 99,213, the Silver row count measured on
the workspace.

The identity drift is a real defect. The simulator builds a fresh patient
record on readmission but reuses the `patient_id`. In Gold, `dim_patients`
keeps the latest version (SCD1), so those 13 patients' earlier encounters
now show someone else's demographics. A real feed would raise this as a
master-patient-index conflict (problem catalog: identity / matching). The
pipeline doesn't detect it yet.

## The vitals

![Vital distributions](vital_distributions.png)

| Vital | Mean | SD | p1 | Median | p99 | Outside band | Impossible |
|---|---|---|---|---|---|---|---|
| heart_rate (bpm) | 97.3 | 26.6 | 39.7 | 97.2 | 152.9 | 481 | 0 |
| spo2 (%) | 94.0 | 3.8 | 85.8 | 94.1 | 102.6 | 551 | 300 |
| resp_rate (/min) | 20.0 | 6.2 | 7.1 | 20.0 | 34.2 | 517 | 23 |
| temp_c (C) | 37.5 | 1.3 | 34.7 | 37.5 | 40.4 | 550 | 0 |
| sbp (mmHg) | 122.5 | 23.5 | 70.6 | 122.6 | 171.0 | 486 | 0 |
| dbp (mmHg) | 74.8 | 15.8 | 40.3 | 74.6 | 106.8 | 498 | 0 |

Three properties of the simulator (`simulator/domain.py:
generate_vitals_event`) show up directly in the data:

1. **Uniform, not clustered.** A heart rate of 56 is as likely as 98 or 139.
   Real vitals cluster around a person's normal value.
2. **No patient.** The share of each vital's variance explained by which
   encounter it belongs to is 7-8%. With about 12 readings per vital per
   encounter, pure noise alone gives about 1/12 = 8%, so patients carry no
   persistent physiology.
3. **No relationships.** Vitals are drawn independently: across the 2,958
   real panels, no pair of vitals correlates beyond |r| = 0.03 (heart rate
   doesn't rise as SpO2 falls). Unit doesn't matter either: mean heart rate
   is 96.4-98.1 bpm in every unit, ICU included.

The pipeline and the agent layer don't depend on realistic vitals: counts,
masking, Genie and the golden set are all unaffected. Anything that
*learns* from vitals does depend on it.

## The anomaly model, tested

`ml/train_anomaly_model.py` trains an IsolationForest on simulator output
and exports `ml/models/vitals_anomaly.onnx`. The agent's
`score_vitals_anomaly` tool uses it to flag readings "where every individual
value is in range but the combination is not". Training only logged the
share of a holdout it flags, which `contamination=0.05` fixes at about 5%.
That says nothing about whether it catches anything.

`ml/evaluate_anomaly_model.py` scores the committed ONNX file with labelled
panels and compares it with the range rule Silver already applies:

![Model vs range rule](model_vs_range_rule.png)

| Test set (n) | Should flag | Model | Range rule |
|---|---|---|---|
| Normal, all in band (2,000) | no | 0.25% | 0% |
| One vital out of band (500) | yes | 26% | 100% |
| Diastolic >= systolic, all in band (500) | yes | 1% | 0% |
| Respiratory distress: HR 125-140, SpO2 88-90, RR 26-30 (500) | yes | 2% | 0% |
| Shock: SBP 85-92, DBP 50-55, HR 125-140 (500) | yes | 7% | 0% |

On the real stream, built as 2,958 complete panels (latest value of each
vital per encounter per minute), the range rule flags 529 and the model
171. The model misses 360 of the rule's 529, and flags 2 the rule doesn't.

Its ROC AUC is 0.86 overall, so the score carries some signal (extreme
corners of the band rank higher). At its operating threshold, though, it
almost never fires. It can't be better than that: the training data
contains no joint structure to learn.

### What changed because of this (round 1)

`agent/tools/anomaly_score.py` now runs deterministic rules on every
reading: alert band, physically impossible value, and diastolic >= systolic
(the impossible pair the model misses 99% of the time). `is_anomaly` is
true if a rule fires **or** the model flags the reading. `decided_by`,
`rule_findings` and `model_status` ("advisory") say which fired, and the
agent's tool description tells it never to present the model's flag alone.
Tests: `tests/test_anomaly_score_tool.py`.

## Round 2: realistic patients, NEWS2, and model v2

Round 1 named three steps. All three are done.

1. **Physiology in the simulator** (`simulator/physiology.py`). Each patient
   gets a baseline around adult normals, with diastolic tied to systolic.
   Acuity by unit makes ICU patients sicker. Deterioration episodes
   (respiratory distress, sepsis, shock, hypertensive crisis) ramp up over
   simulated minutes and move several vitals together. Rare device artifacts
   remain. The event contract is unchanged: labels stay out of the stream.
   Tests: `tests/test_early_warning_and_physiology.py`.
2. **NEWS2** (`agent/tools/early_warning.py`), the published early-warning
   score, as the deterministic baseline. Consciousness and supplemental
   oxygen aren't in the data and score 0.
3. **Model v2**, retrained on stable patients and compared with the range
   rule, NEWS2 and model v1 (`ml/evaluate_anomaly_model.py`).

![Detector comparison](detector_comparison.png)

| Simulated patients (500 per set, 3,000 stable) | Range rule | NEWS2 | Model v1 | Model v2 |
|---|---|---|---|---|
| Stable: should NOT flag | 1.7% | 1.8% | 0% | 4.8% |
| Deterioration caught (8 sets) | 30% | 47% | 6.8% | 70% |
| ROC AUC | 0.64 | 0.82 | 0.61 | 0.90 |

At its default threshold, model v2 raises 2.7 times NEWS2's false alarms,
so the fair comparison sets its threshold to match NEWS2's 1.8%. At that
rate it catches **61% vs NEWS2's 47%**. It catches hypertensive crisis
(70% established), which NEWS2 can't score below a systolic of 220, and
early sepsis (40% vs 22%). NEWS2 stays slightly ahead on early respiratory
distress and early shock.

**Decision (automated in the evaluation):** promote model v2 as a third
layer, at that calibrated threshold, written to
`ml/models/vitals_anomaly_calibration.json`. `score_vitals_anomaly` now
reports three layers in `decided_by`: rules, NEWS2, model. Together they
catch **66%** of deteriorating panels with **3.8%** false alarms on stable
patients. The rules still decide what they can see: the model scores SpO2
80 as normal.

**Limits:**

- Model v2 was trained and tested on the same simulator, so these numbers
  show it learns the physiology we wrote, not that it would work on real
  patients.
- The data on Databricks is still the old uniform stream; NEWS2 fires on
  70% of it and model v2 on 94%. Regenerating it with the new simulator
  needs a workspace login.
- The full tables are in [results.md](../results.md).
