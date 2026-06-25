"""Execution Intelligence routes.

  GET  /api/execution/quality           — per-symbol liquidity + order-book pulse
  POST /api/execution/preview           — given a signal, return SOR + TWAP/VWAP plan
  GET  /api/execution/schedules         — active TWAP/VWAP slice schedules
  POST /api/execution/preview-route     — alias for the dashboard probe
"""
from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_user
from database import get_db
from execution_intel.liquidity import score as liquidity_score
from execution_intel.smart_router import route as sor_route
from execution_intel.twap_vwap import build_schedule, persist as persist_schedule
from execution_intel.order_book import pulse as ob_pulse

router = APIRouter(prefix="/execution", tags=["execution-intelligence"])


async def _resolve_account(db, user_id: str, account_id: str | None):
    if account_id:
        from bson import ObjectId
        try:
            return await db.accounts.find_one({"_id": ObjectId(account_id),
                                               "user_id": user_id})
        except Exception:
            return None
    return await db.accounts.find_one({"user_id": user_id})


@router.get("/quality")
async def execution_quality(
    symbols: str = Query("XAUUSD,BTCUSD", description="Comma-separated symbols"),
    account_id: str | None = Query(None),
    user=Depends(get_current_user),
):
    """Per-symbol liquidity + order-book pulse snapshot for the Execution UI."""
    db = get_db()
    acc = await _resolve_account(db, user["id"], account_id)
    syms = [s.strip().upper() for s in (symbols or "").split(",") if s.strip()]
    if not syms:
        raise HTTPException(status_code=400, detail="symbols required")

    rows = []
    for sym in syms:
        liq = await liquidity_score(db=db, symbol=sym, account=acc)
        pulse = await ob_pulse(db=db, symbol=sym, account=acc)
        rows.append({
            "symbol": sym,
            "liquidity": liq,
            "order_book": pulse,
        })
    return {"items": rows, "account_id": str(acc["_id"]) if acc else None}


@router.post("/preview")
async def execution_preview(payload: dict, user=Depends(get_current_user)):
    """Given a signal `{symbol, action, lot_size}`, return the SOR decision +
    TWAP/VWAP schedule that *would* be used if this trade fires now.
    Pure preview — does NOT persist anything."""
    signal = payload.get("signal") or {}
    if not signal.get("symbol"):
        raise HTTPException(status_code=400, detail="signal.symbol required")

    db = get_db()
    acc = await _resolve_account(db, user["id"], payload.get("account_id"))
    liq = await liquidity_score(db=db, symbol=signal["symbol"], account=acc)
    decision = sor_route(signal=signal, liquidity=liq)

    schedule = None
    if decision["slice_strategy"] in ("TWAP", "VWAP"):
        schedule = build_schedule(
            total_lot=float(signal.get("lot_size") or 0),
            duration_minutes=decision["schedule_minutes"],
            strategy=decision["slice_strategy"],
            slices=4,
        )

    return {
        "signal": signal,
        "liquidity": liq,
        "router": decision,
        "schedule": schedule,
        "account_id": str(acc["_id"]) if acc else None,
    }


@router.get("/schedules")
async def list_schedules(user=Depends(get_current_user)):
    """Active slice schedules for the user — drives the dashboard table."""
    db = get_db()
    cursor = db.slice_schedules.find({
        "user_id": user["id"], "status": "active",
    }).sort("created_at", -1).limit(20)
    docs = await cursor.to_list(length=20)
    items = []
    for d in docs:
        slices = d.get("slices") or []
        done = sum(1 for s in slices if s.get("status") == "filled")
        items.append({
            "schedule_id":      d["schedule_id"],
            "symbol":           d.get("symbol"),
            "action":           d.get("action"),
            "strategy":         d.get("strategy"),
            "total_lot":        d.get("total_lot"),
            "duration_minutes": d.get("duration_minutes"),
            "slices":           slices,
            "filled":           done,
            "total_slices":     len(slices),
            "created_at":       d.get("created_at"),
        })
    return {"items": items}


@router.post("/schedule")
async def create_schedule(payload: dict, user=Depends(get_current_user)):
    """Persist a slice schedule that the bot_runner will execute over time.
    Used by the UI's 'Schedule this trade' action on a preview result."""
    signal = payload.get("signal") or {}
    if not signal.get("symbol") or not signal.get("lot_size"):
        raise HTTPException(status_code=400, detail="signal.symbol + lot_size required")
    db = get_db()
    acc = await _resolve_account(db, user["id"], payload.get("account_id"))
    if not acc:
        raise HTTPException(status_code=404, detail="account not found")

    strategy = payload.get("strategy", "TWAP")
    duration = int(payload.get("duration_minutes") or 10)
    slices_n = int(payload.get("slices") or 4)
    sched = build_schedule(
        total_lot=float(signal["lot_size"]),
        duration_minutes=duration,
        strategy=strategy,
        slices=slices_n,
    )
    sched_id = await persist_schedule(
        db, schedule=sched, signal=signal,
        user_id=user["id"], account_id=str(acc["_id"]),
    )
    return {"schedule_id": sched_id, "schedule": sched}
