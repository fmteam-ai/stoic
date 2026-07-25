"""Phase 4.1 — Subsystem self-monitoring with automatic conservatism.

Every subsystem continuously scores itself (reusing the shadow-health
axes); when the WORST subsystem is unhealthy, live sizing automatically
becomes more conservative (multiplier < 1 applied in bot_runner).
"""
import time

_cache: dict = {}
CACHE_TTL = 15 * 60

SUBSYSTEM_AXES = {
    "learning_engine": "calibration_quality",
    "execution_engine": "execution_quality",
    "risk_engine": "worker_health",
    "broker_engine": "broker_stability",
    "market_engine": "data_freshness",
}


def conservatism_from_scores(scores: dict) -> dict:
    known = {k: v for k, v in scores.items() if v is not None}
    worst_key = min(known, key=known.get) if known else None
    worst = known.get(worst_key) if worst_key else None
    if worst is None or worst >= 60:
        mult = 1.0
    elif worst >= 40:
        mult = 0.75
    else:
        mult = 0.5
    return {"subsystems": scores, "worst": worst_key,
            "worst_score": worst, "multiplier": mult,
            "reason": (f"{worst_key} at {worst} — sizing ×{mult}"
                       if mult < 1.0 else "all subsystems healthy")}


async def subsystem_conservatism(db, user_id: str) -> dict:
    now = time.time()
    cached = _cache.get(user_id)
    if cached and cached[0] > now:
        return cached[1]
    from shadow_health import health_score
    hs = await health_score(db, user_id)
    comps = hs.get("components") or {}
    scores = {sub: comps.get(axis)
              for sub, axis in SUBSYSTEM_AXES.items()}
    out = conservatism_from_scores(scores)
    out["shadow_health_overall"] = hs.get("overall")
    _cache[user_id] = (now + CACHE_TTL, out)
    return out
