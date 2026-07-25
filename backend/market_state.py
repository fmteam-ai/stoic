"""Tier 3 — composite Market State Score (0-100 per axis + Market Health).

One number set every consumer can read: Trend, Volatility (activity +
stability), Liquidity, News Risk, Classification Confidence → Market Health.
"""


async def market_state_score(db, user_id: str) -> dict:
    from market_regime import detect
    snap = await detect(db, user_id)
    trend_axis = (snap.get("trend") or {})
    probs = snap.get("probabilities") or {}
    vol = snap.get("volatility") or {}
    trending = trend_axis.get("axis") in ("trending_up", "trending_down")
    conf = float(trend_axis.get("confidence") or 0)
    trend = round(conf * 100) if trending else round(conf * 40)

    ratio = float(vol.get("ratio") or 1.0)
    volatility = round(min(100, ratio * 50))                # activity level
    vol_stability = round(max(0, 100 - abs(ratio - 1.0) * 60))

    # liquidity — average broker spread-component across fresh scores (24h)
    from datetime import datetime, timezone, timedelta
    fresh_after = datetime.now(timezone.utc) - timedelta(hours=24)
    spreads = []
    async for s in db.broker_intel_scores.find(
            {"at": {"$gte": fresh_after}},
            sort=[("at", -1)], projection={"components": 1,
                                           "account_id": 1}).limit(20):
        v = (s.get("components") or {}).get("spread")
        if v is not None:
            spreads.append(float(v))
    liquidity = round(sum(spreads) / len(spreads)) if spreads else 50

    news_driven = bool((snap.get("news") or {}).get("news_driven"))
    news_risk = 75 if news_driven else 15

    confidence = round((1 - float(probs.get("uncertainty") or 0.5)) * 100)

    axes = {"trend": trend, "volatility": volatility,
            "vol_stability": vol_stability, "liquidity": liquidity,
            "news_risk": news_risk, "confidence": confidence}
    market_health = round(
        trend * 0.2 + vol_stability * 0.2 + liquidity * 0.2
        + (100 - news_risk) * 0.2 + confidence * 0.2)
    return {"scores": axes, "market_health": market_health,
            "regime": {"key": snap.get("key"), "label": snap.get("label"),
                       "top": probs.get("top")},
            "at": snap.get("at")}
