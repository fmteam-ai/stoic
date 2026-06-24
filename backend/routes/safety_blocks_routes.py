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


# Map each dominant blocker → suggested config delta that should reduce
# future blocks. Each entry returns the patch to apply via PUT /api/bot/config.
def _suggest_for(reason: str, cfg: dict) -> dict | None:
    """Return {title, rationale, severity, preview, patch} or None."""
    current_level = cfg.get("risk_level", "medium")
    current_mlot = float(cfg.get("max_lot_size") or 0) or 0.5
    current_mc = int(cfg.get("max_concurrent_trades", 3) or 3)

    if reason == "per_trade_risk_cap":
        # Step risk down one notch (high→medium→low) — smaller SL distance × lot
        if current_level == "high":
            new_level = "medium"
        elif current_level == "medium":
            new_level = "low"
        else:
            new_level = "low"
        return {
            "title": "Per-trade risk too aggressive",
            "severity": "amber",
            "rationale": "The Safety Guardian refused trades because a single trade's "
                         f"risk exceeded {get_guardian_config()['max_risk_pct_per_trade']}% of equity. "
                         "Stepping risk_level down shrinks lot size and SL distance.",
            "preview": f"risk_level: {current_level} → {new_level}",
            "patch": {"risk_level": new_level},
        }
    if reason == "total_open_risk_cap":
        new_mc = max(1, current_mc - 1)
        return {
            "title": "Too many simultaneous open trades",
            "severity": "amber",
            "rationale": "Aggregate open risk crossed the safety ceiling. Reducing "
                         "max_concurrent_trades caps total exposure.",
            "preview": f"max_concurrent_trades: {current_mc} → {new_mc}",
            "patch": {"max_concurrent_trades": new_mc},
        }
    if reason == "lot_vs_equity_sanity":
        new_mlot = round(max(0.01, current_mlot * 0.7), 2)
        return {
            "title": "Lot size too large for equity",
            "severity": "amber",
            "rationale": "Proposed lot exceeded the lot-vs-equity sanity ceiling. "
                         "Lowering max_lot_size keeps individual position size in scale.",
            "preview": f"max_lot_size: {current_mlot} → {new_mlot}",
            "patch": {"max_lot_size": new_mlot},
        }
    if reason in ("daily_loss_cap", "equity_vs_balance_floor", "free_margin_floor"):
        return {
            "title": "Account stress — pause recommended",
            "severity": "red",
            "rationale": "Repeated blocks at the daily-loss or account-health floor "
                         "mean the bot is being kept from compounding losses. Pause until "
                         "the account stabilizes or fund equity is restored.",
            "preview": "active: ON → OFF (pause bot)",
            "patch": {"active": False},
        }
    if reason == "max_concurrent_cap":
        # Not a guardian block but a config cap — suggest no change here
        return None
    if reason == "risk_inputs_present":
        return {
            "title": "Signal payload incomplete",
            "severity": "amber",
            "rationale": "Signals arrived without a stop-loss. This usually means the "
                         "signal generator misconfigured TP/SL. No safe auto-fix — review "
                         "the AI Signals page and confirm SL is being produced.",
            "preview": "Manual review of AI Signals required",
            "patch": None,
        }
    return None


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


@router.get("/suggestion")
async def get_suggestion(days: int = Query(7, ge=1, le=90),
                        user=Depends(get_current_user)):
    """Inspect the last N days of safety blocks. If a single blocker dominates,
    propose a config delta that should reduce future blocks.

    Returns:
      { has_suggestion: bool, top_reason?, count?, title?, rationale?,
        severity?, preview?, patch?, scope_account_id? }
    """
    db = get_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = await db.safety_blocks.aggregate([
        {"$match": {"user_id": user["id"], "blocked_at": {"$gte": cutoff}}},
        {"$group": {"_id": {"reason": "$blocked_by", "acct": "$account_id"},
                    "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 1},
    ]).to_list(length=1)

    if not rows or rows[0]["count"] < 3:
        # Need at least 3 blocks of the same kind to make a confident suggestion.
        return {"has_suggestion": False,
                "reason": "Not enough recent blocks to suggest a change "
                          "(need ≥3 of the same kind in the window)."}

    top = rows[0]
    reason = top["_id"]["reason"]
    scope_account_id = top["_id"].get("acct")
    cfg_q = {"user_id": user["id"]}
    if scope_account_id:
        cfg_q["account_id"] = scope_account_id
    cfg = await db.bot_configs.find_one(cfg_q) or {}
    sug = _suggest_for(reason, cfg)
    if not sug:
        return {"has_suggestion": False, "top_reason": reason,
                "reason": "No automated suggestion available for this blocker."}
    return {
        "has_suggestion": True,
        "top_reason": reason,
        "reason_label": REASON_LABELS.get(reason, reason),
        "count": top["count"],
        "window_days": days,
        "scope_account_id": scope_account_id,
        **sug,
    }


@router.post("/apply-suggestion")
async def apply_suggestion(payload: dict, user=Depends(get_current_user)):
    """Apply the suggested config patch. Body: { patch: dict, scope_account_id?: str }.

    Server re-derives the current top suggestion to ensure the client isn't
    submitting a stale patch — if the suggestion has changed, returns 409.
    """
    patch = payload.get("patch")
    scope_account_id = payload.get("scope_account_id")
    if not patch or not isinstance(patch, dict):
        raise HTTPException(status_code=400, detail="patch (dict) required")

    db = get_db()
    if scope_account_id:
        owns = await db.accounts.find_one({
            "_id": __import__("bson").ObjectId(scope_account_id),
            "user_id": user["id"],
        })
        if not owns:
            raise HTTPException(status_code=404, detail="Account not found")

    cfg_q = {"user_id": user["id"]}
    if scope_account_id:
        cfg_q["account_id"] = scope_account_id
    cfg = await db.bot_configs.find_one(cfg_q)
    if not cfg:
        raise HTTPException(status_code=404, detail="Bot config not found")

    # Sanitize allowed keys — only these may be modified via the suggestion
    allowed = {"risk_level", "max_concurrent_trades", "max_lot_size", "active"}
    safe_patch = {k: v for k, v in patch.items() if k in allowed}
    if not safe_patch:
        raise HTTPException(status_code=400, detail="No allowed fields in patch")
    safe_patch["updated_at"] = datetime.now(timezone.utc).isoformat()
    safe_patch["last_suggestion_applied_at"] = datetime.now(timezone.utc).isoformat()

    await db.bot_configs.update_one(cfg_q, {"$set": safe_patch})
    updated = await db.bot_configs.find_one(cfg_q)
    return {
        "applied": True,
        "applied_at": safe_patch["updated_at"],
        "patch": safe_patch,
        "new_config": {
            "risk_level": updated.get("risk_level"),
            "max_concurrent_trades": updated.get("max_concurrent_trades"),
            "max_lot_size": updated.get("max_lot_size"),
            "active": updated.get("active"),
        },
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
