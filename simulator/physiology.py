"""Patient physiology for the simulator: vitals that behave like a person's.

The first EDA (docs/eda/) found the original generator drew every vital
uniformly across its alert band, independently of the other vitals, the
patient and the unit - so nothing that learns from vitals could work. This
replaces that with:

- a per-patient baseline (heart rate, SpO2, breathing rate, temperature,
  blood pressure) drawn around adult normals, with diastolic tied to
  systolic so blood pressure is correlated as it is in people;
- acuity by unit (ICU sickest, MedSurg least): sicker baselines and more
  frequent deterioration;
- deterioration episodes that ramp up over simulated minutes and move
  several vitals together - respiratory distress, sepsis, shock,
  hypertensive crisis - and sometimes resolve;
- small measurement noise, and rare device artifacts (0 from a disconnected
  probe, SpO2 above 100) that Silver's plausibility check drops.

Labels (`state`, `severity`) exist for evaluation (ml/evaluate_anomaly_model.py)
and never enter the event stream: the event contract is unchanged.

Illustrative engineering numbers, not clinical reference values.
"""

import random
from dataclasses import dataclass

from common.contracts import VITAL_ITEM_TYPES

# Adult baseline: mean, between-patient SD.
_BASELINE = {
    "heart_rate": (78.0, 9.0),
    "spo2": (97.3, 1.1),
    "resp_rate": (15.0, 2.0),
    "temp_c": (36.85, 0.3),
    "sbp": (120.0, 12.0),
}
# Diastolic as a fraction of systolic (mean, SD) - ties the two together.
_DBP_RATIO = (0.63, 0.04)
# Reading-to-reading measurement noise (SD).
_NOISE = {"heart_rate": 3.0, "spo2": 0.6, "resp_rate": 1.0, "temp_c": 0.1, "sbp": 5.0, "dbp": 4.0}

UNIT_ACUITY = {"ICU": 0.7, "ED": 0.45, "Cardiology": 0.35, "Oncology": 0.3, "MedSurg": 0.2}
# How acuity shifts the baseline (added per unit of acuity).
_ACUITY_SHIFT = {"heart_rate": 12.0, "spo2": -1.5, "resp_rate": 3.0, "temp_c": 0.2, "sbp": -4.0}

# Effect of each episode at full severity, added to the baseline.
EPISODES = {
    "respiratory_distress": {"spo2": -11.0, "resp_rate": 14.0, "heart_rate": 30.0, "temp_c": 0.3},
    "sepsis": {"temp_c": 2.0, "heart_rate": 38.0, "resp_rate": 9.0, "sbp": -28.0, "dbp": -16.0, "spo2": -3.0},
    "shock": {"sbp": -42.0, "dbp": -26.0, "heart_rate": 48.0, "resp_rate": 7.0, "spo2": -2.0},
    "hypertensive_crisis": {"sbp": 62.0, "dbp": 34.0, "heart_rate": 6.0},
}
# Chance per simulated minute that a stable patient starts an episode, at acuity 0 and 1.
_ONSET_PER_MINUTE = (0.0008, 0.006)
_RAMP_MINUTES = (8.0, 25.0)  # time to reach full severity
_RESOLVE_PER_MINUTE = 0.01  # chance per minute, once at full severity, of starting to recover
ARTIFACT_PROBABILITY = 0.003


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass
class Physiology:
    baseline: dict
    acuity: float
    state: str = "stable"
    severity: float = 0.0
    _ramp_minutes: float = 15.0
    _recovering: bool = False

    @classmethod
    def new(cls, unit: str, rng=random) -> "Physiology":
        acuity = UNIT_ACUITY.get(unit, 0.3)
        base = {k: rng.gauss(mean, sd) + _ACUITY_SHIFT[k] * acuity for k, (mean, sd) in _BASELINE.items()}
        base["spo2"] = min(base["spo2"], 99.5)
        base["dbp"] = base["sbp"] * rng.gauss(*_DBP_RATIO)
        return cls(baseline=base, acuity=acuity)

    def set_unit(self, unit: str) -> None:
        """Transfers change acuity (and so future onset risk), not the person."""
        self.acuity = UNIT_ACUITY.get(unit, self.acuity)

    def step(self, minutes: float, rng=random) -> None:
        """Advance the patient's state by `minutes` of simulated time."""
        if minutes <= 0:
            return
        if self.state == "stable":
            low, high = _ONSET_PER_MINUTE
            if rng.random() < 1 - (1 - (low + (high - low) * self.acuity)) ** minutes:
                self.state = rng.choice(list(EPISODES))
                self.severity = 0.0
                self._ramp_minutes = rng.uniform(*_RAMP_MINUTES)
                self._recovering = False
            return
        rate = minutes / self._ramp_minutes
        if self._recovering:
            self.severity = max(0.0, self.severity - rate)
            if self.severity == 0.0:
                self.state, self._recovering = "stable", False
            return
        self.severity = min(1.0, self.severity + rate)
        if self.severity >= 1.0 and rng.random() < 1 - (1 - _RESOLVE_PER_MINUTE) ** minutes:
            self._recovering = True

    def expected(self) -> dict:
        """Vitals without noise: baseline plus the current episode's effect."""
        effect = EPISODES.get(self.state, {})
        return {k: self.baseline[k] + effect.get(k, 0.0) * self.severity for k in VITAL_ITEM_TYPES}

    def reading(self, itemid: str, rng=random) -> float:
        if rng.random() < ARTIFACT_PROBABILITY:
            return _artifact(itemid, rng)
        value = self.expected()[itemid] + rng.gauss(0, _NOISE[itemid])
        bounds = {
            "heart_rate": (25, 220),
            "spo2": (60, 100),
            "resp_rate": (4, 60),
            "temp_c": (33.0, 42.5),
            "sbp": (50, 240),
            "dbp": (25, 140),
        }[itemid]
        return round(_clip(value, *bounds), 1)

    def panel(self, rng=random, *, artifacts: bool = False) -> dict:
        """One reading of every vital at this moment (for training and evaluation)."""
        out = {}
        for itemid in VITAL_ITEM_TYPES:
            if artifacts:
                out[itemid] = self.reading(itemid, rng)
            else:
                expected = self.expected()[itemid] + rng.gauss(0, _NOISE[itemid])
                out[itemid] = round(expected, 1)
        if out["spo2"] > 100:
            out["spo2"] = 100.0
        return out


def _artifact(itemid: str, rng) -> float:
    if itemid == "spo2" and rng.random() < 0.5:
        return round(rng.uniform(100.5, 106.0), 1)
    if itemid in ("heart_rate", "resp_rate", "spo2"):
        return 0.0
    if itemid == "temp_c":
        return round(rng.uniform(97.0, 99.5), 1)  # Fahrenheit sent as Celsius
    return 0.0
