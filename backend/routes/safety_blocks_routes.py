"""User-facing endpoints for the Safety Guardian's refused-trade log.

Each row in the `safety_blocks` collection represents a trade the bot tried
to open but was refused by a server-side hard floor. Surfacing these to the
user lets them dial their config to a "0 blocks per week" goldilocks zone
where the bot is aggressive enough to make money but the guardian isn't
constantly slapping it down.
"""
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_user
from database import get_db
from safety_guardian import get_guardian_config

router = APIRouter(prefix="/safety-blocks", tags=["safety-blocks"])


# Human-readable explanation for each blocked_by code — shown in the UI
# alongside the raw code so users understand what the guard was protecting.
REASON_LABELS = {
    "equity_known":              "Account equity unknown / zero",
    "equity_vs_balance_floor":   "Account in deep drawdown (< floor)",
    "free_margin_floor":         "Free margin too low to cover this trade",
    "risk_inputs_present":       "Missing lot / entry / stop-loss in signal",
    "per_trade_risk_cap":        "Single-trade risk exceeds equity %",
    "lot_vs_equity_sanity":      "Lot size oversized vs equity",
    "daily_loss_cap":            "Today's loss already at safety ceiling",
    "total_open_risk_cap":       "Aggregate open risk exceeds equity %",
    "max_concurrent_cap":        "Already at max concurrent trades",
}


def _serialize(doc: dict) -> dict:
    """Strip MongoDB ObjectId + ensure JSON-safe fields."""
    return {
        "id": str(doc.get("_id", "")),
        "user_id": doc.get("user_id"),
        "account_id": doc.get("account_id"),
        "symbol": doc.get("symbol"),
        "action": doc.get("action"),
        "lot_size": doc.get("lot_size"),
        "blocked_by": doc.get("blocked_by"),
        "reason_label": REASON_LABELS.get(doc.get("blocked_by") or "",
                                          doc.get("blocked_by") or "unknown"),
        "blocked_at": doc.get("blocked_at"),
        "audit": doc.get("audit"),
        "context": doc.get("context"),
    }


@router.get("/list")
async def list_blocks(
    limit: int = Query(50, ge=1, le=200),
    days: int = Query(7, ge=1, le=90),
    user=Depends(get_current_user),
):
    """Return the user's recent safety blocks, newest first."""
    db = get_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = await db.safety_blocks.find({
        "user_id": user["id"], "blocked_at": {"$gte": cutoff},
    }).sort("blocked_at", -1).limit(limit).to_list(length=limit)
    return {
        "blocks": [_serialize(r) for r in rows],
        "total_in_window": await db.safety_blocks.count_documents({
            "user_id": user["id"], "blocked_at": {"$gte": cutoff},
        }),
        "window_days": days,
    }


@router.get("/stats")
async def block_stats(
    days: int = Query(7, ge=1, le=90),
    user=Depends(get_current_user),
):
    """Aggregated block stats for charts:
      - by_reason: count per blocked_by code
      - by_day:    count per UTC day for a sparkline
    """
    db = get_db()
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff = cutoff_dt.isoformat()

    # Count per reason
    by_reason_rows = await db.safety_blocks.aggregate([
        {"$match": {"user_id": user["id"], "blocked_at": {"$gte": cutoff}}},
        {"$group": {"_id": "$blocked_by", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
    ]).to_list(length=20)
    by_reason = [
        {"blocked_by": r["_id"], "count": r["count"],
         "label": REASON_LABELS.get(r["_id"] or "", r["_id"] or "unknown")}
        for r in by_reason_rows
    ]

    # Bucket by UTC day for sparkline. We compute in Python since
    # `blocked_at` is stored as ISO string (not a BSON datetime), so we can't
    # use $dateTrunc reliably without a conversion stage.
    rows = await db.safety_blocks.find({
        "user_id": user["id"], "blocked_at": {"$gte": cutoff},
    }, projection={"blocked_at": 1}).to_list(length=10000)
    buckets: dict[str, int] = {}
    for r in rows:
        ts = str(r.get("blocked_at") or "")[:10]  # YYYY-MM-DD
        if ts:
            buckets[ts] = buckets.get(ts, 0) + 1
    # Fill missing days with 0 so the sparkline is continuous
    by_day = []
    for i in range(days, -1, -1):
        d = (datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%d")
        by_day.append({"date": d, "count": buckets.get(d, 0)})

    return {
        "by_reason": by_reason,
        "by_day": by_day,
        "total": sum(r["count"] for r in by_reason),
        "thresholds": get_guardian_config(),
        "window_days": days,
    }


@router.get("/{block_id}")
async def block_detail(block_id: str, user=Depends(get_current_user)):
    """Return a single block with full audit trail. User can only view their own."""
    db = get_db()
    from bson import ObjectId
    try:
        oid = ObjectId(block_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid block id")
    doc = await db.safety_blocks.find_one({"_id": oid, "user_id": user["id"]})
    if not doc:
        raise HTTPException(status_code=404, detail="Block not found")
    return _serialize(doc)
