from datetime import datetime, timezone, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from bson import ObjectId

from auth import get_current_user
from database import get_db
from ai_signals import analyze_symbol
from route_utils import parse_object_id
from routes.bot_routes import _config_filter

router = APIRouter(prefix="/signals", tags=["signals"])


def _serialize(doc: dict) -> dict:
    doc["id"] = str(doc.pop("_id"))
    return doc


@router.get("")
async def list_signals(limit: int = 50, user=Depends(get_current_user)):
    db = get_db()
    cursor = db.signals.find({"user_id": user["id"]}).sort("created_at", -1).limit(limit)
    docs = await cursor.to_list(length=limit)
    return [_serialize(d) for d in docs]


@router.post("/generate")
async def generate_signal(payload: dict, account_id: Optional[str] = None,
                          user=Depends(get_current_user)):
    """Generate AI signal for one symbol on demand.

    `account_id` (query param) — pick which bot_config to read risk_level from.
    Omitted → default profile (account_id is None). Lets the Signals UI scope
    the generation to a specific bot when the user has several.
    """
    symbol = (payload.get("symbol") or "").upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol required")
    db = get_db()
    cfg = await db.bot_configs.find_one(_config_filter(user["id"], account_id)) or {}
    risk_level = payload.get("risk_level") or cfg.get("risk_level", "medium")

    try:
        signal = await analyze_symbol(symbol, risk_level)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI analysis failed: {e}")

    signal["user_id"] = user["id"]
    signal["consumed"] = False
    signal["created_at"] = datetime.now(timezone.utc).isoformat()
    if account_id:
        signal["account_id"] = account_id
    result = await db.signals.insert_one(signal)
    signal["_id"] = result.inserted_id
    return _serialize(signal)


@router.post("/generate-all")
async def generate_all(account_id: Optional[str] = None,
                       user=Depends(get_current_user)):
    """Generate signals for all configured symbols of a chosen bot.

    `account_id` (query param) selects which bot_config drives the symbol
    list + risk_level. Omitted → default profile.
    """
    db = get_db()
    cfg = await db.bot_configs.find_one(_config_filter(user["id"], account_id))
    if not cfg or not cfg.get("symbols"):
        raise HTTPException(status_code=400, detail="No symbols configured")
    risk_level = cfg.get("risk_level", "medium")
    results = []
    for sym in cfg["symbols"]:
        try:
            sig = await analyze_symbol(sym, risk_level)
            sig["user_id"] = user["id"]
            sig["consumed"] = False
            sig["created_at"] = datetime.now(timezone.utc).isoformat()
            if account_id:
                sig["account_id"] = account_id
            r = await db.signals.insert_one(sig)
            sig["_id"] = r.inserted_id
            results.append(_serialize(sig))
        except Exception as e:
            results.append({"symbol": sym, "error": str(e)})
    return {"generated": results, "account_id": account_id}


@router.delete("/{signal_id}")
async def delete_signal(signal_id: str, user=Depends(get_current_user)):
    db = get_db()
    await db.signals.delete_one({"_id": parse_object_id(signal_id, "Signal"), "user_id": user["id"]})
    return {"ok": True}


@router.delete("")
async def bulk_clear_signals(
    scope: str = "all",
    older_than_days: int | None = None,
    user=Depends(get_current_user),
):
    """Bulk-delete signals for the current user.

    Query params:
      scope=all        → every signal owned by the user (default)
      scope=hold       → only HOLD signals (noise cleanup)
      scope=non_hold   → only BUY/SELL signals
      older_than_days  → restrict to signals created more than N days ago

    Examples:
      DELETE /api/signals                      → clear all
      DELETE /api/signals?scope=hold           → clear HOLDs only
      DELETE /api/signals?older_than_days=7    → clear signals > 7 days old
      DELETE /api/signals?scope=hold&older_than_days=1  → tidy HOLDs older than 1d
    """
    db = get_db()
    q: dict = {"user_id": user["id"]}
    if scope == "hold":
        q["action"] = "HOLD"
    elif scope == "non_hold":
        q["action"] = {"$in": ["BUY", "SELL"]}
    elif scope != "all":
        raise HTTPException(
            status_code=400,
            detail="scope must be one of: all, hold, non_hold",
        )
    if older_than_days is not None:
        if older_than_days < 0:
            raise HTTPException(status_code=400, detail="older_than_days must be ≥ 0")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
        q["created_at"] = {"$lt": cutoff}
    result = await db.signals.delete_many(q)
    return {"ok": True, "deleted": result.deleted_count, "scope": scope, "older_than_days": older_than_days}

