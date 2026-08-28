"""Nitro Eligibility — reusable scoring subsystem.

Nitro means MAXIMUM EXECUTION SELECTIVITY, never maximum risk: any
serious execution problem ⇒ NO NITRO. Score 0-100 from component
qualities; missing evidence scores CONSERVATIVELY low."""
from datetime import datetime, timedelta, timezone

from strategies.nitro.config import (ENABLED_MIN, HARD_COMPONENTS,
                                     HARD_FLOOR, REDUCED_MIN, WEIGHTS)


def score_to_status(score: float, components: dict) -> str:
    if any(components.get(c, 0) <= HARD_FLOOR for c in HARD_COMPONENTS):
        return "NITRO_PAUSED"
    if score >= ENABLED_MIN:
        return "NITRO_ENABLED"
    if score >= REDUCED_MIN:
        return "NITRO_REDUCED"
    return "NITRO_PAUSED"


def compute_score(components: dict) -> dict:
    """Pure weighted score — components are 0-100, missing = 0."""
    score = round(sum(WEIGHTS[k] * float(components.get(k) or 0)
                      for k in WEIGHTS), 1)
    return {"score": score, "components": components,
            "status": score_to_status(score, components),
            "thresholds": {"enabled_min": ENABLED_MIN,
                           "reduced_min": REDUCED_MIN,
                           "hard_floor": HARD_FLOOR},
            "note": "thresholds are initial values pending empirical "
                    "calibration"}


def _clamp(v: float) -> float:
    return max(0.0, min(100.0, v))


async def eligibility(db, user_id: str,
                      account: dict | None = None) -> dict:
    """Evidence-backed component scores for a user/account context."""
    comps: dict = {}
    # latency quality — p95 total from the last 7 days
    from latency_profiler import latency_summary
    ls = await latency_summary(db, days=7, user_id=user_id)
    p95s = [g["total_ms"]["p95"] for g in ls.get("groups", [])
            if (g.get("total_ms") or {}).get("p95") is not None]
    if p95s:
        worst = max(p95s)
        comps["latency_quality"] = _clamp(100 - worst / 30)  # 3s → 0
    else:
        comps["latency_quality"] = 40  # no evidence → conservative
    unknown = ls.get("unknown_rate")
    comps["infrastructure_health"] = _clamp(
        100 - (unknown or 0.5) * 100)
    from degraded_intelligence import status as degraded_status
    deg = await degraded_status(db)
    if deg.get("critical_failing"):
        comps["infrastructure_health"] = 0
    elif deg.get("failing"):
        comps["infrastructure_health"] = min(
            comps["infrastructure_health"], 60)
    # clock health folds into infrastructure (hard for Nitro)
    if account and (account.get("agent_clock") or {}).get(
            "status") == "SKEW_SUSPECTED":
        comps["infrastructure_health"] = 0
    # spread quality — freshness of the account tick/spread feed
    spread_age = None
    if account:
        try:
            d = datetime.fromisoformat(
                str(account.get("spreads_updated_at")))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            spread_age = (datetime.now(timezone.utc) - d).total_seconds()
        except (TypeError, ValueError):
            pass
    comps["spread_quality"] = (_clamp(100 - (spread_age / 12))
                               if spread_age is not None else 40)
    # broker quality — realized slippage from execution alpha history
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    n = bad = 0
    async for a in db.execution_alpha_decisions.find(
            {"user_id": user_id, "at": {"$gte": since}},
            {"mode": 1}).limit(500):
        n += 1
        bad += 1 if a.get("mode") in ("SKIP", "WAIT") else 0
    comps["broker_quality"] = (_clamp(100 - (bad / n) * 150)
                               if n >= 10 else 60)
    comps["slippage_quality"] = comps["broker_quality"]
    comps["liquidity"] = comps["spread_quality"]
    # market/regime — degraded market memory means unknown regime
    comps["market_quality"] = 70 if deg.get("mode") == "NORMAL" else 40
    comps["regime_compatibility"] = comps["market_quality"]
    comps = {k: round(v, 1) for k, v in comps.items()}
    return compute_score(comps)
