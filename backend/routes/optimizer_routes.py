"""AI Strategy Optimizer routes — suggest-only trade-review recommendations.

POST /api/optimizer/analyze                — run on-demand analysis (24/48h)
GET  /api/optimizer/report                 — latest report for a scope
GET  /api/optimizer/summary                — latest report per scope (dashboard card)
POST /api/optimizer/report/{rid}/rec/{recid}/apply    — apply one recommendation
POST /api/optimizer/report/{rid}/rec/{recid}/dismiss  — dismiss one recommendation
"""
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from database import get_db
from route_utils import parse_object_id
from ai_optimizer import (
    analyze_account, get_latest_report, apply_recommendation,
    serialize_report, CACHE_MINUTES,
)

router = APIRouter(prefix="/optimizer", tags=["optimizer"])


async def _assert_account_owned(db, user_id: str, account_id: Optional[str]):
    if not account_id:
        return
    owns = await db.accounts.find_one(
        {"_id": parse_object_id(account_id, "Account"), "user_id": user_id}
    )
    if not owns:
        raise HTTPException(status_code=404, detail="Account not found")


@router.post("/analyze")
async def run_analysis(account_id: Optional[str] = None, window: int = 24,
                       force: bool = False, user=Depends(get_current_user)):
    """On-demand analysis. Returns a cached report if one for the same scope
    was generated < CACHE_MINUTES ago (unless force=true) — protects the LLM
    key from double-click spam."""
    db = get_db()
    await _assert_account_owned(db, user["id"], account_id)
    if not force:
        cache_cut = (datetime.now(timezone.utc) - timedelta(minutes=CACHE_MINUTES)).isoformat()
        recent = await db.optimizer_reports.find_one(
            {"user_id": user["id"], "account_id": account_id,
             "created_at": {"$gte": cache_cut}},
            sort=[("created_at", -1)],
        )
        if recent:
            out = serialize_report(recent)
            out["cached"] = True
            return out
    return await analyze_account(user["id"], account_id, window, source="manual")


@router.get("/report")
async def latest_report(account_id: Optional[str] = None,
                        user=Depends(get_current_user)):
    report = await get_latest_report(user["id"], account_id)
    return report or {"exists": False, "account_id": account_id}


@router.get("/summary")
async def optimizer_summary(user=Depends(get_current_user)):
    """Dashboard card payload — latest report per scope with pending counts."""
    db = get_db()
    accounts = await db.accounts.find({"user_id": user["id"]}).to_list(200)
    labels = {str(a["_id"]): (a.get("label") or a.get("broker") or "Account")
              for a in accounts}

    # Latest report per (account_id) scope via aggregation
    pipeline = [
        {"$match": {"user_id": user["id"]}},
        {"$sort": {"created_at": -1}},
        {"$group": {"_id": "$account_id", "doc": {"$first": "$$ROOT"}}},
    ]
    rows = []
    async for g in db.optimizer_reports.aggregate(pipeline):
        d = g["doc"]
        acct_id = d.get("account_id")
        pending = [r for r in (d.get("recommendations") or []) if r.get("status") == "pending"]
        rows.append({
            "report_id": str(d["_id"]),
            "account_id": acct_id,
            "account_label": labels.get(acct_id, "Default Profile") if acct_id else "Default Profile",
            "verdict": d.get("verdict"),
            "headline": d.get("headline"),
            "win_rate": (d.get("stats") or {}).get("win_rate"),
            "total_trades": (d.get("stats") or {}).get("total_trades"),
            "total_pnl": (d.get("stats") or {}).get("total_pnl"),
            "pending_recommendations": len(pending),
            "window_hours": d.get("window_hours"),
            "source": d.get("source"),
            "created_at": d.get("created_at"),
        })
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return {"reports": rows,
            "total_pending": sum(r["pending_recommendations"] for r in rows)}


async def _get_report_and_rec(db, user_id: str, report_id: str, rec_id: str):
    report = await db.optimizer_reports.find_one(
        {"_id": parse_object_id(report_id, "Report"), "user_id": user_id}
    )
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    rec = next((r for r in (report.get("recommendations") or [])
                if r.get("id") == rec_id), None)
    if not rec:
        raise HTTPException(status_code=404, detail="Recommendation not found")
    return report, rec


@router.post("/report/{report_id}/rec/{rec_id}/apply")
async def apply_rec(report_id: str, rec_id: str, user=Depends(get_current_user)):
    db = get_db()
    report, rec = await _get_report_and_rec(db, user["id"], report_id, rec_id)
    if rec.get("status") != "pending":
        raise HTTPException(status_code=409, detail=f"Recommendation already {rec.get('status')}")
    audit = await apply_recommendation(user["id"], report, rec)
    await db.optimizer_reports.update_one(
        {"_id": report["_id"], "recommendations.id": rec_id},
        {"$set": {"recommendations.$.status": "applied",
                  "recommendations.$.applied_at": audit["applied_at"]}},
    )
    return {"applied": True, "rec_id": rec_id, "audit": audit}


@router.post("/report/{report_id}/rec/{rec_id}/dismiss")
async def dismiss_rec(report_id: str, rec_id: str, user=Depends(get_current_user)):
    db = get_db()
    report, rec = await _get_report_and_rec(db, user["id"], report_id, rec_id)
    if rec.get("status") != "pending":
        raise HTTPException(status_code=409, detail=f"Recommendation already {rec.get('status')}")
    await db.optimizer_reports.update_one(
        {"_id": report["_id"], "recommendations.id": rec_id},
        {"$set": {"recommendations.$.status": "dismissed",
                  "recommendations.$.dismissed_at": datetime.now(timezone.utc).isoformat()}},
    )
    return {"dismissed": True, "rec_id": rec_id}
