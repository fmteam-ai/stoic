"""Broker execution intelligence API (Phase 2)."""
from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from database import get_db

router = APIRouter(prefix="/broker-intel", tags=["broker-intel"])


@router.get("/qualification")
async def broker_qualification(user=Depends(get_current_user)):
    """Phase 2.1 — evidence-based broker certification matrix."""
    from broker_qualification import qualification_matrix
    return await qualification_matrix(get_db(), user["id"])


@router.get("")
async def broker_intel(user=Depends(get_current_user)):
    """Live execution score for every connected broker + routing pick."""
    from broker_intel import score_account
    from differentiation import certify
    from execution_timing import timing_stats
    db = get_db()
    out = []
    async for acc in db.accounts.find(
            {"user_id": user["id"], "status": {"$ne": "deleted"},
             "dormant": {"$ne": True}, "harness": {"$ne": True}}):
        res = await score_account(db, acc)
        res["timing"] = await timing_stats(db, res["account_id"])
        res["certification"] = certify(res["score"], res["provisional"],
                                       res["fills_measured"])
        out.append(res)
    scored = [r for r in out if r["score"] is not None and not r["provisional"]]
    best = max(scored, key=lambda r: r["score"]) if scored else None
    return {"brokers": sorted(out, key=lambda r: -(r["score"] or -1)),
            "best_execution": (
                {"account_id": best["account_id"], "label": best["label"],
                 "score": best["score"]} if best else None)}


@router.get("/forecast")
async def broker_forecast(account_id: str, symbol: str | None = None,
                          user=Depends(get_current_user)):
    """Tier 9 — pre-trade execution quality forecast for an account."""
    from bson import ObjectId
    from broker_intel import execution_forecast
    db = get_db()
    acc = await db.accounts.find_one(
        {"_id": ObjectId(account_id), "user_id": user["id"]})
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    return await execution_forecast(db, acc, symbol)


@router.get("/certification")
async def broker_certification(user=Depends(get_current_user)):
    """Phase 8 — broker certification tiers from measured live execution."""
    from broker_intel import score_account
    from differentiation import certify
    db = get_db()
    rows = []
    async for acc in db.accounts.find(
            {"user_id": user["id"], "status": {"$ne": "deleted"},
             "dormant": {"$ne": True}, "harness": {"$ne": True}}):
        res = await score_account(db, acc)
        rows.append({"account_id": res["account_id"], "label": res["label"],
                     "broker": acc.get("broker"), "score": res["score"],
                     "fills_measured": res["fills_measured"],
                     "certification": certify(res["score"],
                                              res["provisional"],
                                              res["fills_measured"])})
    return {"brokers": rows,
            "tiers": {"CERTIFIED": "score ≥ 80, ≥10 measured fills",
                      "ACCEPTABLE": "score 55-79 — adequate, monitor",
                      "DEGRADED": "score < 55 — avoid routing",
                      "PROVISIONAL": "not enough measured fills yet"}}
