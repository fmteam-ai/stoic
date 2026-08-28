"""Execution Eligibility Engine (v62.3) — ONE evidence collector,
STRATEGY-SPECIFIC policies. Scalper / Fast Scalp / Nitro have different
minimum execution quality; "Nitro eligibility" is only Nitro's policy,
never the shared validator.

  ExecutionEligibilityEngine
  ├── SCALPER_POLICY     minimum execution quality = MODERATE
  ├── FAST_SCALP_POLICY  minimum execution quality = HIGH
  └── NITRO_POLICY       minimum execution quality = EXTREME"""
from strategies.nitro.config import (ENABLED_MIN, HARD_COMPONENTS,
                                     HARD_FLOOR, REDUCED_MIN)

POLICIES = {
    "sniper": None,  # latency-insensitive — no execution-quality gate
    "scalper": {"label": "MODERATE", "min_score": 60, "reduced_min": None,
                "hard_floor": 30,
                "hard_components": ("spread_quality", "broker_quality")},
    "fast_scalp": {"label": "HIGH", "min_score": 75, "reduced_min": None,
                   "hard_floor": 40,
                   "hard_components": ("spread_quality", "latency_quality",
                                       "infrastructure_health")},
    "nitro_scalper": {"label": "EXTREME", "min_score": ENABLED_MIN,
                      "reduced_min": REDUCED_MIN,
                      "hard_floor": HARD_FLOOR,
                      "hard_components": HARD_COMPONENTS},
}


def apply_policy(strategy_id: str, score: float, components: dict) -> dict:
    """Pure per-strategy policy decision over shared evidence scores."""
    sid = str(strategy_id or "").lower()
    policy = POLICIES.get(sid)
    base = {"strategy_id": sid, "score": score}
    if policy is None:
        return {**base, "policy": "NONE", "status": "ELIGIBLE",
                "reason": "strategy has no execution-quality requirement"}
    floored = sorted(c for c in policy["hard_components"]
                     if float(components.get(c) or 0) <= policy["hard_floor"])
    out = {**base, "policy": policy["label"],
           "min_score": policy["min_score"],
           "hard_floor": policy["hard_floor"],
           "hard_components": list(policy["hard_components"])}
    if floored:
        return {**out, "status": "INELIGIBLE",
                "reason": f"hard_floor: {','.join(floored)}"}
    if score >= policy["min_score"]:
        return {**out, "status": "ELIGIBLE", "reason": "ok"}
    if policy["reduced_min"] is not None and score >= policy["reduced_min"]:
        return {**out, "status": "REDUCED",
                "reason": "score below minimum — reduced participation"}
    return {**out, "status": "INELIGIBLE",
            "reason": f"score {score} < required {policy['min_score']}"}


async def execution_eligibility(db, strategy_id: str, user_id: str,
                                account: dict | None = None) -> dict:
    """Evidence collection is shared (strategies/nitro/eligibility);
    the DECISION is strategy-specific. Passing the actual account keeps
    account-level evidence (agent clock, spread freshness) in scope."""
    from strategies.nitro.eligibility import eligibility
    ev = await eligibility(db, user_id, account)
    return {**apply_policy(strategy_id, ev["score"], ev["components"]),
            "components": ev["components"],
            "evidence_account_scoped": bool(account)}
