# Clinical Knowledge Base (demo content)

**These are plausible, generic, illustrative entries written for this
portfolio project's demo — they are NOT real hospital policy, are not
sourced from any real institution, and must never be treated as clinical
guidance.** They exist to give
[`agent/tools/knowledge_search.py`](../../agent/tools/knowledge_search.py)'s
Databricks Vector Search integration real text content to index and search
over, in the same spirit as `data/reference/synthea_sample/` being real but
synthetic patient data — see `README.md`'s "Dataset sourcing" section.

## ICU discharge criteria

A patient may be considered for ICU-to-floor transfer when, for a sustained
period of at least 4 hours without escalation of support: heart rate and
blood pressure remain within the unit's stable range on no new vasopressor
requirement; SpO2 remains at or above 92% on stable or decreasing
supplemental oxygen; respiratory rate stays within a normal range without new
work-of-breathing findings; and mental status is at the patient's baseline.
Transfer requires a documented attending sign-off and a receiving-unit bed
confirmation before the patient physically leaves the ICU.

## Sepsis alert escalation protocol

A sepsis alert fires when two or more SIRS-consistent findings (temperature,
heart rate, respiratory rate, white blood cell count) coincide with a
suspected infection source. On alert: draw blood cultures and initiate broad-
spectrum antibiotics within one hour of recognition, obtain a serum lactate,
begin the initial fluid bolus per the unit's weight-based protocol, and
re-assess perfusion and lactate clearance within 3 hours. Any alert not
acknowledged by a clinician within 15 minutes auto-escalates to the charge
nurse and the rapid-response team.

## Fall-risk assessment protocol

Every inpatient is scored on admission and each shift using a standardized
fall-risk instrument (e.g., mobility, prior falls, medications affecting
balance or alertness, elimination needs). A high-risk score triggers: a
color-coded wristband and door signage, bed/chair alarms enabled, hourly
rounding, non-slip footwear, and a bed positioned in its lowest locked
position with two side rails up. Risk score is re-evaluated after any
change in medication, procedure, or mental status.

## Medication reconciliation on transfer

At every transition of care (admission, unit-to-unit transfer, and
discharge), the current medication list must be reconciled against the
home/prior-unit list by a pharmacist or the accepting clinician. Any
discrepancy (omission, duplication, dose or route change) is documented with
a stated reason before the new unit's first scheduled administration.
Reconciliation is a hard gate for discharge — it cannot be deferred to a
follow-up visit.

## Isolation precaution levels

Three precaution levels apply based on suspected or confirmed transmission
route: **Contact** (gown and gloves for room entry; dedicated or
disinfected equipment), **Droplet** (surgical mask within a defined distance
of the patient, adds to Contact requirements if both apply), and
**Airborne** (a fit-tested N95 or PAPR plus a negative-pressure room with the
door kept closed). Precaution level and rationale are posted at the room
entrance and reassessed when culture/test results return or symptoms
resolve.

## Rapid response team activation criteria

Any staff member — not only the assigned nurse or physician — may activate a
rapid response for a hospitalized patient showing acute deterioration:
a sustained heart rate or respiratory rate outside the unit's normal range,
a new or worsening altered mental status, SpO2 below 90% despite supplemental
oxygen, or simply "staff member is worried" with no single objective
trigger met. Activation does not require prior physician approval, and the
responding team's assessment and plan are documented in the chart within
one hour of the call.
