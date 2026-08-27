"""Outcome Attribution API — /api/attribution"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from database import get_db

router = APIRouter(prefix="/attribution", tags=["attribution"])

_LESSONS = {
    "ALPHA_ERROR": "the signal itself was wrong — a strategy problem",
    "REGIME_ERROR": "market regime shifted during the hold",
    "TIMING_ERROR": "fills landed too late after the signal",
    "SIZING_ERROR": "position sizing was constrained or off",
    "EXECUTION_ERROR": "slippage/execution quality cost you, not the idea",
    "BROKER_ERROR": "the broker (rejects/requotes/incidents) hurt results",
    "INFRASTRUCTURE_ERROR": "retries, ghosts or degraded intents interfered",
    "NEWS_SHOCK": "high-impact news hit inside the trade window",
    "CORRELATION_ERROR": "too many correlated positions lost together",
    "NORMAL_VARIANCE": "normal trading variance — no fix needed",
}


def _scope(user) -> dict:
    return {} if user.get("role") == "admin" else {"user_id": user["id"]}


@router.get("/summary")
async def attribution_summary_ep(days: int = 30,
                                 strategy: str | None = None,
                                 user=Depends(get_current_user)):
    db = get_db()
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=max(1, min(int(days), 365)))).isoformat()
    q = {**_scope(user), "closed_at": {"$gte": cutoff}}
    if strategy:
        q["strategy_id"] = strategy
    cats: dict = {}
    strategies: dict = {}
    total, losses, wins, alpha_clean = 0, 0, 0, 0
    async for o in db.trade_outcomes.find(q, {"_id": 0}).limit(5000):
        total += 1
        r = float(o.get("result_r") or 0)
        if r < 0:
            losses += 1
        elif r > 0:
            wins += 1
        if o.get("alpha_clean"):
            alpha_clean += 1
        for cat, w in (o.get("attribution") or {}).items():
            c = cats.setdefault(cat, {"weighted_r": 0.0, "loss_r": 0.0,
                                      "trades": 0, "primary_count": 0})
            c["weighted_r"] += w * r
            if r < 0:
                c["loss_r"] += w * r
            c["trades"] += 1
        if o.get("primary_category"):
            cats.setdefault(o["primary_category"],
                            {"weighted_r": 0.0, "loss_r": 0.0,
                             "trades": 0, "primary_count": 0}
                            )["primary_count"] += 1
        s = strategies.setdefault(o.get("strategy_id") or "manual",
                                  {"trades": 0, "sum_r": 0.0,
                                   "noise_r": 0.0})
        s["trades"] += 1
        s["sum_r"] += r
        if r < 0:
            s["noise_r"] += r * sum(
                (o.get("attribution") or {}).get(c, 0) for c in
                ("EXECUTION_ERROR", "BROKER_ERROR",
                 "INFRASTRUCTURE_ERROR", "NEWS_SHOCK"))
    for c in cats.values():
        c["weighted_r"] = round(c["weighted_r"], 2)
        c["loss_r"] = round(c["loss_r"], 2)
    for s in strategies.values():
        s["sum_r"] = round(s["sum_r"], 2)
        s["noise_r"] = round(s["noise_r"], 2)
    worst = min(cats.items(), key=lambda kv: kv[1]["loss_r"])[0] \
        if cats else None
    return {"days": days, "total": total, "wins": wins, "losses": losses,
            "alpha_clean": alpha_clean, "categories": cats,
            "strategies": strategies, "worst_category": worst,
            "lesson": (f"Biggest drag: {worst.replace('_', ' ')} — "
                       f"{_LESSONS.get(worst, '')}") if worst else
            "No attributed outcomes in this window yet."}


@router.get("/trades")
async def attribution_trades_ep(limit: int = 50,
                                strategy: str | None = None,
                                user=Depends(get_current_user)):
    db = get_db()
    q = _scope(user)
    if strategy:
        q["strategy_id"] = strategy
    lim = max(1, min(int(limit), 200))
    return {"outcomes": [o async for o in db.trade_outcomes.find(
        q, {"_id": 0}).sort("closed_at", -1).limit(lim)]}


@router.get("/trades/{trade_id}")
async def attribution_trade_ep(trade_id: str,
                               user=Depends(get_current_user)):
    db = get_db()
    doc = await db.trade_outcomes.find_one(
        {"trade_id": trade_id, **_scope(user)}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="No attribution yet")
    return doc


@router.post("/backfill")
async def attribution_backfill_ep(user=Depends(get_current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    from outcome_attribution import attribute_missing
    n = await attribute_missing(get_db(), limit=500)
    return {"attributed": n}
