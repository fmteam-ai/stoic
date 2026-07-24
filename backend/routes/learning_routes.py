"""Autopilot #7/#8 — learning records + failure taxonomy API."""
from fastapi import APIRouter, Depends

from auth import get_current_user
from database import get_db

router = APIRouter(prefix="/learning", tags=["learning"])


@router.get("/records")
async def learning_records(days: int = 30, category: str | None = None,
                           limit: int = 50,
                           user=Depends(get_current_user)):
    from datetime import datetime, timedelta, timezone
    db = get_db()
    since = (datetime.now(timezone.utc)
             - timedelta(days=max(1, min(days, 365)))).isoformat()
    q = {"user_id": user["id"], "closed_at": {"$gte": since}}
    if category:
        q["failure.category"] = category
    rows = []
    async for r in db.learning_records.find(q).sort(
            "closed_at", -1).limit(max(1, min(limit, 200))):
        r["id"] = str(r.pop("_id"))
        rows.append(r)
    return {"records": rows, "count": len(rows)}


@router.get("/failure-summary")
async def failure_taxonomy(days: int = 30, user=Depends(get_current_user)):
    from learning_record import failure_summary
    db = get_db()
    return await failure_summary(db, user["id"],
                                 days=max(1, min(days, 365)))


@router.get("/speeds")
async def learning_speeds(user=Depends(get_current_user)):
    """Autopilot #11 — the three learning speeds with live evidence."""
    from datetime import datetime, timedelta, timezone
    db = get_db()
    uid = user["id"]
    now = datetime.now(timezone.utc)
    acc_ids = [str(a["_id"]) async for a in db.accounts.find(
        {"user_id": uid}, {"_id": 1})]
    timing_24h = await db.execution_timing_stats.count_documents(
        {"account_id": {"$in": acc_ids}, "at": {"$gte": now - timedelta(hours=24)}})
    guards = await db.auto_guards.count_documents(
        {"user_id": uid, "active": True})
    alloc = await db.strategy_allocations.find_one({"user_id": uid})
    ol = await db.online_learning.find_one({"user_id": uid})
    last_run = await db.learning_runs.find_one(
        {"user_id": uid}, sort=[("at", -1)])
    shadows = await db.shadow_models.count_documents({"user_id": uid})
    return {"speeds": [
        {"tier": "fast", "horizon": "seconds – hours",
         "levers": ["position size (adaptive multipliers)",
                    "spread limits & execution timing",
                    "confidence threshold (uncertainty gate)",
                    "session participation"],
         "bounded_by": "pre-approved clamps: multiplier [0.1×, 2×], "
                       "risk floor/cap, spread tiers — never widened by learning",
         "live": {"execution_timing_events_24h": timing_24h}},
        {"tier": "medium", "horizon": "daily – weekly",
         "levers": ["strategy & symbol allocation", "regime thresholds",
                    "exit parameters", "auto-guards"],
         "bounded_by": "shadow comparison required — guards need net ≥ +$100 "
                       "over 14d of real trades before applying",
         "live": {"active_auto_guards": guards,
                  "dynamic_allocation_trades": (alloc or {}).get("n_trades"),
                  "online_learning_last": str((ol or {}).get("at") or "never")}},
        {"tier": "slow", "horizon": "weekly – monthly",
         "levers": ["model retrains & feature sets", "strategy logic",
                    "portfolio methodology"],
         "bounded_by": "full staged promotion: replay → shadow → validation "
                       "→ approval; freeze after losing streaks",
         "live": {"last_learning_run": str((last_run or {}).get("at") or "never"),
                  "last_run_frozen": (last_run or {}).get("frozen"),
                  "shadow_models": shadows}},
    ]}


@router.get("/safety-invariants")
async def safety_invariants(user=Depends(get_current_user)):
    """Autopilot #14 — dangerous self-learning behaviors and what prevents
    each one."""
    db = get_db()
    pending = await db.governed_changes.count_documents(
        {"user_id": user["id"], "status": "pending"})
    inv = [
        ("optimize after every loss",
         "loss reviews run on a 24h cooldown with a hard evidence bar "
         "(net ≥ +$100 shadow-tested, losses avoided ≥ 2× wins missed)"),
        ("increase lot size to recover",
         "drawdown multiplier only SHRINKS size (0.4× at 10% DD); "
         "risk raises are governance-gated"),
        ("remove stop-losses",
         "every order ships with an SL; no learner has a stop-removal lever"),
        ("widen drawdown limits",
         "classified AGGRESSIVE by change governance — requires user approval"),
        ("chase recent performance",
         "allocation blends long-window metrics with sample-size confidence "
         "(≥10 trades) — last-few-trades noise is damped"),
        ("train and deploy on the same data",
         "walk-forward TimeSeriesSplit OOS AUC weighting + holdout validation"),
        ("promote a model on a short winning streak",
         "staged promotion (replay → shadow → validation → approval) + "
         "learning freeze after losing streaks"),
        ("use public sentiment as the only signal",
         "news/sentiment feeds act as gates and bias only — never entries"),
        ("let an LLM issue broker commands",
         "Claude is narration/classification only; orders come from "
         "deterministic engines through the risk chain"),
    ]
    return {"invariants": [{"behavior": b, "prevented_by": p,
                            "status": "enforced"} for b, p in inv],
            "pending_governance_approvals": pending,
            "principle": "the bot may automatically become more conservative;"
                         " becoming more aggressive requires approval"}
