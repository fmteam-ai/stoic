"""PAMM trade verdict engine (review §10) — APPROVE / REDUCE / REJECT with
a scaled approved risk instead of a boolean gate.

Scaling: each enabled limit contributes a factor from its remaining
headroom. Utilization below SOFT_ZONE costs nothing; above it the factor
falls linearly to 0 at the threshold. REJECT below MIN_FACTOR."""
from modules.pamm.risk.states import (RISK_REDUCED_FACTOR, op_state_of)

SOFT_ZONE = 0.5   # start reducing once a limit is >50% utilized
MIN_FACTOR = 0.1  # below 10% of requested risk → outright REJECT


def _factor(value: float, threshold: float) -> float:
    headroom = max(0.0, 1.0 - float(value) / float(threshold))
    return max(0.0, min(1.0, headroom / SOFT_ZONE))


async def trade_verdict(db, program: dict,
                        requested_risk_pct: float) -> dict:
    from modules.pamm.risk import trading_allowed
    from modules.pamm.risk.limits import evaluate_program

    requested = round(float(requested_risk_pct), 4)
    if requested <= 0:
        raise ValueError("requested_risk_pct must be positive")
    base = {"program_id": program["program_id"],
            "requested_risk_pct": requested}

    allowed, reason = await trading_allowed(db, program)
    if not allowed:
        return {**base, "verdict": "REJECT", "approved_risk_pct": 0.0,
                "reason": reason, "factors": []}

    ev = await evaluate_program(db, program)
    factors = []
    for c in ev["checks"]:
        if not c["enabled"] or c["value"] is None:
            continue
        f = _factor(c["value"], c["threshold"])
        factors.append({"limit": c["limit"], "value": c["value"],
                        "threshold": c["threshold"],
                        "factor": round(f, 3)})
    scale = min([f["factor"] for f in factors], default=1.0)
    if op_state_of(program) == "risk_reduced":
        scale *= RISK_REDUCED_FACTOR
        factors.append({"limit": "op_state:risk_reduced",
                        "factor": RISK_REDUCED_FACTOR})

    approved = round(requested * min(scale, 1.0), 4)
    if scale <= MIN_FACTOR:
        verdict, approved = "REJECT", 0.0
    elif scale >= 0.999:
        verdict = "APPROVE"
    else:
        verdict = "REDUCE"
    return {**base, "verdict": verdict, "approved_risk_pct": approved,
            "scale": round(min(scale, 1.0), 4), "reason": "ok",
            "factors": factors, "nav": ev.get("nav")}
