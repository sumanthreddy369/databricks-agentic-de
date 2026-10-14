"""NEWS2 (National Early Warning Score 2, Royal College of Physicians, 2017)
for one panel of vitals - the deterministic, explainable baseline any
learned deterioration detector has to beat (ml/evaluate_anomaly_model.py).

Scored parameters: respiration rate, SpO2 (scale 1), systolic blood pressure,
pulse, temperature. Two NEWS2 parameters aren't in this data and are scored
0: supplemental oxygen (assumed room air) and consciousness (assumed alert).
So a score here is a lower bound on the real NEWS2.

Trigger: an aggregate of 5 or more, or 3 in any single parameter.
Illustrative implementation for a synthetic-data project, not a clinical tool.
"""


def _band(value: float, cuts: list[tuple[float, int]], top: int) -> int:
    """cuts: [(upper bound inclusive, points), ...] ascending; above all -> top."""
    for upper, points in cuts:
        if value <= upper:
            return points
    return top


def parameter_scores(panel: dict) -> dict[str, int]:
    rr = round(float(panel["resp_rate"]))
    spo2 = round(float(panel["spo2"]))
    sbp = round(float(panel["sbp"]))
    hr = round(float(panel["heart_rate"]))
    temp = round(float(panel["temp_c"]), 1)
    return {
        "resp_rate": _band(rr, [(8, 3), (11, 1), (20, 0), (24, 2)], 3),
        "spo2": _band(spo2, [(91, 3), (93, 2), (95, 1)], 0),
        "sbp": _band(sbp, [(90, 3), (100, 2), (110, 1), (219, 0)], 3),
        "heart_rate": _band(hr, [(40, 3), (50, 1), (90, 0), (110, 1), (130, 2)], 3),
        "temp_c": _band(temp, [(35.0, 3), (36.0, 1), (38.0, 0), (39.0, 1)], 2),
    }


def news2(panel: dict) -> dict:
    scores = parameter_scores(panel)
    total = sum(scores.values())
    red = [name for name, points in scores.items() if points == 3]
    if total >= 7:
        risk = "high"
    elif total >= 5:
        risk = "medium"
    elif red:
        risk = "low-medium"
    else:
        risk = "low"
    return {
        "score": total,
        "parameters": scores,
        "single_parameter_3": red,
        "risk": risk,
        "trigger": total >= 5 or bool(red),
    }
