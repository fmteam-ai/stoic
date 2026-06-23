from datetime import datetime, timezone, timedelta
import os
from fastapi import APIRouter, Depends, HTTPException
from bson import ObjectId

from auth import get_current_user
from database import get_db
from models import BotConfigUpdate, BotConfigOut
from risk import get_profile
from intelligence_counters import get_window_24h as intel_window_24h
from strategy_presets import list_presets, get_preset
from user_presets import (
    list_user_presets, create_user_preset, delete_user_preset, get_user_preset,
)

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
        "weekly_drawdown_pct": 7.0,
        "weekly_drawdown_enabled": True,
        "spread_filter_enabled": False,
        "max_spread_pips": {"XAUUSD": 50.0, "BTCUSD": 100.0},
        "auto_tune_enabled": True,
        "slippage_veto_enabled": True,
        "max_slippage_pips": {"XAUUSD": 20.0, "BTCUSD": 80.0},
        "anti_tilt_enabled": True,
        "anti_tilt_consecutive_losses": 3,
        "anti_tilt_freeze_hours": 4,
        "trade_of_day_cap": 1,
        "asia_session_skip_xau": True,
        "sl_cooldown_enabled": True,
        "sl_cooldown_minutes": 45,
        "pre_news_protect_enabled": True,
        "pre_news_protect_minutes": 5,
        "aggressive_mode": False,
        "min_confidence_override": 0,
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
        "weekly_drawdown_pct": cfg.get("weekly_drawdown_pct", 7.0),
        "weekly_drawdown_enabled": cfg.get("weekly_drawdown_enabled", True),
        "spread_filter_enabled": cfg.get("spread_filter_enabled", False),
        "max_spread_pips": cfg.get("max_spread_pips") or {"XAUUSD": 50.0, "BTCUSD": 100.0},
        "auto_tune_enabled": cfg.get("auto_tune_enabled", True),
        "slippage_veto_enabled": cfg.get("slippage_veto_enabled", True),
        "max_slippage_pips": cfg.get("max_slippage_pips") or {"XAUUSD": 20.0, "BTCUSD": 80.0},
        "anti_tilt_enabled": cfg.get("anti_tilt_enabled", True),
        "anti_tilt_consecutive_losses": cfg.get("anti_tilt_consecutive_losses", 3),
        "anti_tilt_freeze_hours": cfg.get("anti_tilt_freeze_hours", 4),
        "trade_of_day_cap": cfg.get("trade_of_day_cap", 1),
        "asia_session_skip_xau": cfg.get("asia_session_skip_xau", True),
        "sl_cooldown_enabled": cfg.get("sl_cooldown_enabled", True),
        "sl_cooldown_minutes": cfg.get("sl_cooldown_minutes", 45),
        "pre_news_protect_enabled": cfg.get("pre_news_protect_enabled", True),
        "pre_news_protect_minutes": cfg.get("pre_news_protect_minutes", 5),
        "aggressive_mode": cfg.get("aggressive_mode", False),
        "min_confidence_override": cfg.get("min_confidence_override", 0),
        "active_preset": cfg.get("active_preset"),
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
    # PATCH semantics — only fields explicitly sent by the client are written.
    # Lets the UI / API do partial updates without nuking other settings.
    update = payload.model_dump(exclude_unset=True)

    # Field-specific coercions
    if "symbols" in update:
        update["symbols"] = [s.upper() for s in (update["symbols"] or [])]
    if "max_spread_pips" in update:
        update["max_spread_pips"] = {
            str(k).upper(): float(v) for k, v in (update["max_spread_pips"] or {}).items()
        }
    if "max_slippage_pips" in update:
        update["max_slippage_pips"] = {
            str(k).upper(): float(v) for k, v in (update["max_slippage_pips"] or {}).items()
        }
    update["updated_at"] = datetime.now(timezone.utc).isoformat()

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


@router.get("/presets")
async def get_strategy_presets(user=Depends(get_current_user)):
    """Return built-in presets + the user's own saved presets, in one payload."""
    custom = await list_user_presets(user["id"])
    return {"presets": list_presets(), "custom": custom}


@router.post("/preset/{key}")
async def apply_strategy_preset(key: str, user=Depends(get_current_user)):
    """Overlay a named preset (built-in or user-owned) onto the user's bot_config.

    Only the fields defined in the preset are touched. risk_level, symbols,
    drawdown limits, and per-symbol caps are preserved.
    """
    db = get_db()
    preset_label: str
    overlay_config: dict
    if key.startswith("custom:"):
        # user-owned preset → key is "custom:{preset_id}"
        preset_id = key.split(":", 1)[1]
        custom = await get_user_preset(user_id=user["id"], preset_id=preset_id)
        if not custom:
            raise HTTPException(status_code=404, detail="Custom preset not found")
        preset_label = custom["name"]
        overlay_config = custom["config"]
    else:
        builtin = get_preset(key)
        if not builtin:
            raise HTTPException(status_code=404, detail=f"Preset '{key}' not found")
        preset_label = builtin["label"]
        overlay_config = builtin["config"]

    await _get_or_create_config(db, user["id"])  # ensure doc exists
    overlay = {**overlay_config, "active_preset": key,
               "updated_at": datetime.now(timezone.utc).isoformat()}
    await db.bot_configs.update_one(
        {"user_id": user["id"]}, {"$set": overlay}, upsert=True
    )
    cfg = await db.bot_configs.find_one({"user_id": user["id"]})
    return {"applied": key, "label": preset_label, "config": _serialize(cfg)}


@router.post("/my-presets")
async def save_user_preset(payload: dict, user=Depends(get_current_user)):
    """Save the user's CURRENT bot_config behaviour knobs as a named preset.

    Body: { name, description? }
    """
    db = get_db()
    cfg = await _get_or_create_config(db, user["id"])
    try:
        preset = await create_user_preset(
            user_id=user["id"],
            name=payload.get("name", ""),
            description=payload.get("description", ""),
            config=cfg,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return preset


@router.delete("/my-presets/{preset_id}")
async def delete_my_preset(preset_id: str, user=Depends(get_current_user)):
    ok = await delete_user_preset(user_id=user["id"], preset_id=preset_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Custom preset not found")
    return {"deleted": True, "id": preset_id}


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
    profile_min_conf = profile.get("min_confidence", 65)
    override = int(cfg.get("min_confidence_override") or 0)
    min_conf = override if (0 < override < 100) else profile_min_conf

    # Look up EA heartbeat freshness for live accounts (5-min cutoff matches bot_runner)
    accounts = await db.accounts.find({"user_id": user["id"]}).to_list(length=20)
    HEARTBEAT_FRESH_SEC = 300
    fresh_live_account = None
    stalest_age = None
    for a in accounts:
        if a.get("mode") == "paper":
            fresh_live_account = a   # paper is always "connected"
            break
        hb = a.get("last_heartbeat")
        if not hb:
            continue
        try:
            from datetime import datetime as _dt
            hb_dt = _dt.fromisoformat(hb.replace("Z", "+00:00"))
            age_sec = (datetime.now(timezone.utc) - hb_dt).total_seconds()
            if age_sec <= HEARTBEAT_FRESH_SEC:
                fresh_live_account = a
                break
            if stalest_age is None or age_sec < stalest_age:
                stalest_age = int(age_sec)
        except Exception:
            continue

    # Per-symbol signal cooldown — replicate bot_runner internal state
    cooldown_remaining_sec = None
    last_signal_symbol = (last_signal or {}).get("symbol")
    if last_tick_iso and last_signal_symbol:
        try:
            from datetime import datetime as _dt
            last_tick_dt = _dt.fromisoformat(last_tick_iso.replace("Z", "+00:00"))
            cooldown_min = int(os.environ.get("BOT_SIGNAL_COOLDOWN_MIN", "15"))
            eligible_at = last_tick_dt + timedelta(minutes=cooldown_min)
            remaining = (eligible_at - datetime.now(timezone.utc)).total_seconds()
            if remaining > 0:
                cooldown_remaining_sec = int(remaining)
        except Exception:
            pass

    why_no_trade = None
    if not cfg.get("active"):
        why_no_trade = "Bot is stopped — start it from Bot Config"
    elif not cfg.get("auto_execute"):
        why_no_trade = "Auto-execute is OFF — signals generated but trades require manual click"
    elif not accounts:
        why_no_trade = "No MT5 account connected — add one in Accounts to enable execution"
    elif fresh_live_account is None:
        if stalest_age is not None:
            why_no_trade = (
                f"EA heartbeat stale ({stalest_age}s ago) — live execution needs a "
                f"heartbeat within 5 min. Check MT5 Algo Trading button."
            )
        else:
            why_no_trade = "EA never connected — install EmergentTradingBridge.ex5 on MT5"
    elif last_signal is None:
        why_no_trade = "No signals generated yet — first tick pending"
    elif last_action == "HOLD":
        why_no_trade = f"AI returned HOLD ({last_conf}% conf) — no high-conviction setup"
    elif last_conf is not None and last_conf < min_conf:
        why_no_trade = f"Confidence {last_conf}% below {min_conf}% threshold ({profile.get('label')} profile)"
    elif last_veto:
        why_no_trade = f"Vetoed: {last_veto}"
    elif cooldown_remaining_sec and cooldown_remaining_sec > 0:
        mins, secs = divmod(cooldown_remaining_sec, 60)
        why_no_trade = (
            f"Symbol on cooldown for {mins}m {secs}s — last signal fired recently. "
            f"Next eligible tick when cooldown clears."
        )
    # else: trade likely fired — no explanation needed

    # If a trade was attempted recently but the broker rejected it, surface the error.
    # This trumps cooldown messages because it's the actually-actionable failure.
    recent_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    recent_failed = await db.trades.find_one(
        {"user_id": user["id"], "status": "failed", "opened_at": {"$gte": recent_cutoff}},
        sort=[("opened_at", -1)],
    )
    if recent_failed and recent_failed.get("error"):
        err = str(recent_failed["error"])
        retcode_hint = ""
        if "10016" in err:
            retcode_hint = " (INVALID_STOPS — broker rejects: SL/TP too close to entry. Try a different broker or widen risk profile.)"
        elif "10013" in err:
            retcode_hint = " (INVALID_REQUEST — bad order params; check symbol name in EA)"
        elif "10014" in err:
            retcode_hint = " (INVALID_VOLUME — lot size below broker minimum)"
        elif "10018" in err:
            retcode_hint = " (MARKET_CLOSED — symbol not tradable right now)"
        elif "10019" in err:
            retcode_hint = " (NO_MONEY — insufficient margin / account balance)"
        elif "10027" in err:
            retcode_hint = " (AUTOTRADING_DISABLED on broker side — enable in MT5 server settings)"
        why_no_trade = f"Broker rejected last trade: {err}{retcode_hint}"

    next_tick_in = None
    if seconds_since_tick is not None:
        next_tick_in = max(0, interval - (seconds_since_tick % interval))

    # Open trades count
    open_trades = await db.trades.count_documents({
        "user_id": user["id"], "status": {"$in": ["pending", "open"]}
    })

    intelligence = await intel_window_24h(user["id"])

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
        "cooldown_remaining_sec": cooldown_remaining_sec,
        "intelligence": intelligence,
    }
