"""Broker execution intelligence API (Phase 2)."""
from fastapi import APIRouter, Depends

from auth import get_current_user
from database import get_db

router = APIRouter(prefix="/broker-intel", tags=["broker-intel"])


@router.get("")
async def broker_intel(user=Depends(get_current_user)):
    """Live execution score for every connected broker + routing pick."""
    from broker_intel import score_account
    from execution_timing import timing_stats
    db = get_db()
    out = []
    async for acc in db.accounts.find(
            {"user_id": user["id"], "status": {"$ne": "deleted"},
             "dormant": {"$ne": True}, "harness": {"$ne": True}}):
        res = await score_account(db, acc)
        res["timing"] = await timing_stats(db, res["account_id"])
        out.append(res)
    scored = [r for r in out if r["score"] is not None and not r["provisional"]]
    best = max(scored, key=lambda r: r["score"]) if scored else None
    return {"brokers": sorted(out, key=lambda r: -(r["score"] or -1)),
            "best_execution": (
                {"account_id": best["account_id"], "label": best["label"],
                 "score": best["score"]} if best else None)}
