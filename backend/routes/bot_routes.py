from datetime import datetime, timezone
import os
from fastapi import APIRouter, Depends, HTTPException
from bson import ObjectId

from auth import get_current_user
from database import get_db
from models import BotConfigUpdate, BotConfigOut
from risk import get_profile

router = APIRouter(prefix="/bot", tags=["bot"])


async def _get_or_create_config(db, user_id: str) -> dict:
    cfg = await db.bot_configs.find_one({"user_id": user_id})
    if cfg:
        return cfg
    new_cfg = {
        "user_id": user_id,
        "risk_level": "medium",
        "symbols": ["XAUUSD", "BTCUSD"],
        "active": False,
        "max_concurrent_trades": 3,
        "auto_execute": True,
        "breakeven_enabled": True,
        "breakeven_trigger_r": 1.0,
        "partial_close_enabled": True,
        "partial_close_trigger_r": 1.0,
        "partial_close_fraction": 0.5,
        "trailing_enabled": True,
        "trailing_start_r": 1.5,
        "trailing_distance_r": 0.7,
        "daily_drawdown_pct": 3.0,
        "daily_drawdown_enabled": True,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    result = await db.bot_configs.insert_one(new_cfg)
    new_cfg["_id"] = result.inserted_id
    return new_cfg


def _serialize(cfg: dict) -> dict:
    return {
        "id": str(cfg["_id"]),
        "user_id": cfg["user_id"],
        "risk_level": cfg.get("risk_level", "medium"),
        "symbols": cfg.get("symbols", []),
        "active": cfg.get("active", False),
        "max_concurrent_trades": cfg.get("max_concurrent_trades", 3),
        "auto_execute": cfg.get("auto_execute", True),
        "breakeven_enabled": cfg.get("breakeven_enabled", True),
        "breakeven_trigger_r": cfg.get("breakeven_trigger_r", 1.0),
        "partial_close_enabled": cfg.get("partial_close_enabled", True),
        "partial_close_trigger_r": cfg.get("partial_close_trigger_r", 1.0),
        "partial_close_fraction": cfg.get("partial_close_fraction", 0.5),
        "trailing_enabled": cfg.get("trailing_enabled", True),
        "trailing_start_r": cfg.get("trailing_start_r", 1.5),
        "trailing_distance_r": cfg.get("trailing_distance_r", 0.7),
        "daily_drawdown_pct": cfg.get("daily_drawdown_pct", 3.0),
        "daily_drawdown_enabled": cfg.get("daily_drawdown_enabled", True),
        "updated_at": cfg.get("updated_at"),
    }


@router.get("/config")
async def get_config(user=Depends(get_current_user)):
    db = get_db()
    cfg = await _get_or_create_config(db, user["id"])
    return _serialize(cfg)


@router.put("/config")
async def update_config(payload: BotConfigUpdate, user=Depends(get_current_user)):
    db = get_db()
    update = {
        "risk_level": payload.risk_level,
        "symbols": [s.upper() for s in payload.symbols],
        "active": payload.active,
        "max_concurrent_trades": payload.max_concurrent_trades,
        "auto_execute": payload.auto_execute,
        "breakeven_enabled": payload.breakeven_enabled,
        "breakeven_trigger_r": payload.breakeven_trigger_r,
        "partial_close_enabled": payload.partial_close_enabled,
        "partial_close_trigger_r": payload.partial_close_trigger_r,
        "partial_close_fraction": payload.partial_close_fraction,
        "trailing_enabled": payload.trailing_enabled,
        "trailing_start_r": payload.trailing_start_r,
        "trailing_distance_r": payload.trailing_distance_r,
        "daily_drawdown_pct": payload.daily_drawdown_pct,
        "daily_drawdown_enabled": payload.daily_drawdown_enabled,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.bot_configs.update_one(
        {"user_id": user["id"]}, {"$set": update}, upsert=True
    )
    cfg = await db.bot_configs.find_one({"user_id": user["id"]})
    return _serialize(cfg)


@router.post("/start")
async def start_bot(user=Depends(get_current_user)):
    db = get_db()
    await db.bot_configs.update_one(
        {"user_id": user["id"]},
        {"$set": {"active": True, "updated_at": datetime.now(timezone.utc).isoformat()}},
        upsert=True,
    )
    return {"active": True}


@router.post("/stop")
async def stop_bot(user=Depends(get_current_user)):
    db = get_db()
    await db.bot_configs.update_one(
        {"user_id": user["id"]},
        {"$set": {"active": False, "updated_at": datetime.now(timezone.utc).isoformat()}},
    )
    return {"active": False}


@router.get("/status")
async def get_bot_status(user=Depends(get_current_user)):
    """Return rich bot runtime status: last signal, last tick, why-no-trade, next tick ETA."""
    db = get_db()
    cfg = await _get_or_create_config(db, user["id"])
    profile = get_profile(cfg.get("risk_level", "medium"))
    interval = int(os.environ.get("BOT_LOOP_INTERVAL_SEC", "60"))

    cursor = db.signals.find({"user_id": user["id"]}).sort("created_at", -1).limit(1)
    docs = await cursor.to_list(length=1)
    last_signal = docs[0] if docs else None

    last_tick_iso = (last_signal or {}).get("created_at") if last_signal else None
    seconds_since_tick = None
    if last_tick_iso:
        try:
            dt = datetime.fromisoformat(str(last_tick_iso).replace("Z", "+00:00"))
            seconds_since_tick = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
        except Exception:
            seconds_since_tick = None

    # Derive why-no-trade explanation
    last_action = (last_signal or {}).get("action")
    last_conf = (last_signal or {}).get("confidence")
    last_veto = (last_signal or {}).get("veto_reason")
    min_conf = profile.get("min_confidence", 65)
    why_no_trade = None
    if not cfg.get("active"):
        why_no_trade = "Bot is stopped — start it from Bot Config"
    elif not cfg.get("auto_execute"):
        why_no_trade = "Auto-execute is OFF — signals generated but trades require manual click"
    elif last_signal is None:
        why_no_trade = "No signals generated yet — first tick pending"
    elif last_action == "HOLD":
        why_no_trade = f"AI returned HOLD ({last_conf}% conf) — no high-conviction setup"
    elif last_conf is not None and last_conf < min_conf:
        why_no_trade = f"Confidence {last_conf}% below {min_conf}% threshold ({profile.get('label')} profile)"
    elif last_veto:
        why_no_trade = f"Vetoed: {last_veto}"
    # else: trade likely fired — no explanation needed

    next_tick_in = None
    if seconds_since_tick is not None:
        next_tick_in = max(0, interval - (seconds_since_tick % interval))

    # Open trades count
    open_trades = await db.trades.count_documents({
        "user_id": user["id"], "status": {"$in": ["pending", "open"]}
    })

    return {
        "active": cfg.get("active", False),
        "auto_execute": cfg.get("auto_execute", True),
        "risk_level": cfg.get("risk_level", "medium"),
        "symbols": cfg.get("symbols", []),
        "min_confidence": min_conf,
        "max_concurrent_trades": cfg.get("max_concurrent_trades", 3),
        "open_trades": open_trades,
        "tick_interval_seconds": interval,
        "seconds_since_last_tick": seconds_since_tick,
        "next_tick_in_seconds": next_tick_in,
        "last_signal": {
            "symbol": (last_signal or {}).get("symbol"),
            "action": last_action,
            "confidence": last_conf,
            "veto_reason": last_veto,
            "reasoning": (last_signal or {}).get("reasoning"),
            "created_at": last_tick_iso,
        } if last_signal else None,
        "why_no_trade": why_no_trade,
    }
