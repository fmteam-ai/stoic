"""PAMM trade verdict engine (review §10) — APPROVE / REDUCE / REJECT with
a scaled approved risk instead of a boolean gate.

Each risk dimension scales through its own configurable CURVE (review v53
§3): LINEAR, EXPONENTIAL (steeper near the boundary), STEP (discrete
tiers), LOGISTIC (steep sigmoid) or HARD (all-or-nothing). Utilization
below SOFT_ZONE costs nothing on continuous curves."""
import math

from modules.pamm.risk.states import RISK_REDUCED_FACTOR, op_state_of

SOFT_ZONE = 0.5   # continuous curves start reducing above 50% utilization
MIN_FACTOR = 0.1  # below 10% of requested risk → outright REJECT


def curve_factor(value: float, threshold: float,
                 curve: str = "linear") -> float:
    """Scaling factor in [0,1] from utilization of one limit."""
    u = max(0.0, float(value) / float(threshold))
    if curve == "hard":
        return 1.0 if u < 1.0 else 0.0
    if curve == "step":
        if u < 0.5:
            return 1.0
        if u < 0.75:
            return 0.5
        if u < 0.9:
            return 0.25
        return 0.0
    if curve == "logistic":  # ≈1 below soft zone, 0.5 at 75%, ≈0 near cap
        return round(1.0 / (1.0 + math.exp(12.0 * (u - 0.75))), 6)
    lin = max(0.0, min(1.0, (1.0 - u) / SOFT_ZONE))
    if curve == "exponential":  # increasingly conservative near the cap
        return lin ** 2
    return lin  # linear


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
                "reason": reason, "primary_reason": reason,
                "limiting_factor": "trading_blocked", "risk_factor": 0.0,
                "factors": []}

    ev = await evaluate_program(db, program)
    factors = []
    for c in ev["checks"]:
        if not c["enabled"] or c["value"] is None:
            continue
        f = curve_factor(c["value"], c["threshold"],
                         c.get("curve", "linear"))
        factors.append({"limit": c["limit"], "value": c["value"],
                        "threshold": c["threshold"],
                        "curve": c.get("curve", "linear"),
                        "factor": round(f, 3)})
    scale = min([f["factor"] for f in factors], default=1.0)
    if op_state_of(program) == "risk_reduced":
        scale *= RISK_REDUCED_FACTOR
        factors.append({"limit": "op_state:risk_reduced",
                        "curve": "fixed", "factor": RISK_REDUCED_FACTOR})

    approved = round(requested * min(scale, 1.0), 4)
    limiting = min(factors, key=lambda f: f["factor"]) if factors else None
    if scale <= MIN_FACTOR:
        verdict, approved = "REJECT", 0.0
    elif scale >= 0.999:
        verdict = "APPROVE"
    else:
        verdict = "REDUCE"
    # reason hierarchy (review v54 §4) → feeds Outcome Attribution later
    primary = ("full_headroom" if verdict == "APPROVE"
               else f"{limiting['limit']}_headroom" if limiting else "ok")
    return {**base, "verdict": verdict, "approved_risk_pct": approved,
            "scale": round(min(scale, 1.0), 4), "reason": "ok",
            "primary_reason": primary,
            "limiting_factor": limiting["limit"] if limiting else None,
            "risk_factor": limiting["factor"] if limiting else 1.0,
            "factors": factors, "nav": ev.get("nav")}
