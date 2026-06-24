from datetime import datetime, timezone, timedelta
import os
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from bson import ObjectId

from auth import get_current_user
from database import get_db
from models import BotConfigUpdate, BotConfigOut
from risk import get_profile, compute_lot_for_account
from intelligence_counters import get_window_24h as intel_window_24h
from strategy_presets import list_presets, get_preset
from user_presets import (
    list_user_presets, create_user_preset, delete_user_preset, get_user_preset,
)

router = APIRouter(prefix="/bot", tags=["bot"])


def _config_filter(user_id: str, account_id: Optional[str]) -> dict:
    """Match the user's bot_config doc for either the default scope (no
    account_id / account_id null) or a specific account.
    Stored docs may omit `account_id` (legacy) — treat as default.
    """
    if account_id:
        return {"user_id": user_id, "account_id": account_id}
    return {
        "user_id": user_id,
        "$or": [{"account_id": None}, {"account_id": {"$exists": False}}],
    }


async def _get_or_create_config(db, user_id: str, account_id: Optional[str] = None) -> dict:
    cfg = await db.bot_configs.find_one(_config_filter(user_id, account_id))
    if cfg:
        return cfg
    new_cfg = {
        "user_id": user_id,
        "account_id": account_id,  # None = default profile
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
        "max_lot_size": 0.0,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    result = await db.bot_configs.insert_one(new_cfg)
    new_cfg["_id"] = result.inserted_id
    return new_cfg


def _serialize(cfg: dict) -> dict:
    return {
        "id": str(cfg["_id"]),
        "user_id": cfg["user_id"],
        "account_id": cfg.get("account_id"),
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
        "max_lot_size": float(cfg.get("max_lot_size") or 0.0),
        "active_preset": cfg.get("active_preset"),
        "updated_at": cfg.get("updated_at"),
    }


@router.get("/sizing-preview")
async def sizing_preview(
    account_id: Optional[str] = None,
    symbol: str = "XAUUSD",
    user=Depends(get_current_user),
):
    """Live preview of the lot size the bot would open at various confidence
    levels — using the user's actual equity, risk profile, and max_lot_size.

    Lets the user dial in a sensible `max_lot_size` BEFORE risking real money,
    instead of trial-and-error on actual signals. Powers the "Position Sizing
    Preview" panel on the BotConfig page.
    """
    db = get_db()

    # Pick the account: requested → first connected → first owned.
    acct = None
    if account_id:
        acct = await db.accounts.find_one(
            {"_id": ObjectId(account_id), "user_id": user["id"]}
        )
        if not acct:
            raise HTTPException(status_code=404, detail="Account not found")
    if not acct:
        acct = await db.accounts.find_one(
            {"user_id": user["id"], "status": "connected"}
        )
    if not acct:
        acct = await db.accounts.find_one({"user_id": user["id"]})
    if not acct:
        return {
            "symbol": symbol, "rows": [], "warnings": ["No account connected"],
        }

    cfg = await _get_or_create_config(db, user["id"], account_id)
    profile = get_profile(cfg.get("risk_level", "medium"))
    max_lot_cap = float(cfg.get("max_lot_size") or 0.0)

    # Realistic SL distances per symbol — matches the typical ATR-derived SL
    # the bot uses live. Keeps the preview honest without faking the calc.
    typical_entry_sl = {
        "XAUUSD": (4100.0, 4115.0),    # ~150 pips
        "BTCUSD": (62000.0, 61500.0),  # ~500 pips
        "ETHUSD": (3200.0, 3175.0),
        "XAGUSD": (32.0, 31.7),
    }
    entry, sl = typical_entry_sl.get(symbol.upper(), (1.0, 0.985))

    rows = []
    for conf in (55, 60, 65, 70, 75, 80, 85, 90):
        sized = compute_lot_for_account(
            account=acct, symbol=symbol,
            entry_price=entry, stop_loss=sl,
            confidence_pct=float(conf), profile=profile,
        )
        kelly_f = float(sized.get("kelly_f") or 0)
        kelly_cap = float(profile.get("kelly_cap") or 0)
        absolute_lot = float(sized["lot_size"])
        if max_lot_cap > 0 and kelly_cap > 0:
            scale = min(kelly_f / kelly_cap, 1.0) if kelly_f > 0 else 0.0
            scaled = max(round(max_lot_cap * scale, 2), 0.01)
            effective = min(absolute_lot, scaled)
        else:
            effective = (min(absolute_lot, max_lot_cap)
                         if max_lot_cap > 0 else absolute_lot)
        rows.append({
            "confidence_pct": conf,
            "kelly_f": kelly_f,
            "absolute_lot": absolute_lot,
            "effective_lot": effective,
            "risk_amount_usd": sized.get("risk_amount_usd"),
            "below_min_confidence": conf < profile["min_confidence"],
        })

    return {
        "account_id": str(acct["_id"]),
        "account_label": acct.get("label"),
        "account_equity": float(acct.get("equity") or acct.get("balance") or 0),
        "account_type": acct.get("account_type"),
        "symbol": symbol,
        "risk_level": cfg.get("risk_level", "medium"),
        "risk_pct": profile["risk_pct"],
        "kelly_cap": profile["kelly_cap"],
        "min_confidence": profile["min_confidence"],
        "max_lot_size": max_lot_cap,
        "sl_pips": rows[0].get("sl_pips") if rows else None,
        "scenario": {
            "entry_price": entry,
            "stop_loss": sl,
            "sl_distance_price": abs(entry - sl),
        },
        "rows": rows,
    }


@router.get("/config")
async def get_config(account_id: Optional[str] = None, user=Depends(get_current_user)):
    """Fetch the bot config for the default scope (account_id=None) or a
    specific connected account. Creates a fresh config doc on first read.
    """
    db = get_db()
    if account_id:
        # Validate the account belongs to this user before touching the config.
        owns = await db.accounts.find_one(
            {"_id": ObjectId(account_id), "user_id": user["id"]}
        )
        if not owns:
            raise HTTPException(status_code=404, detail="Account not found")
    cfg = await _get_or_create_config(db, user["id"], account_id)
    return _serialize(cfg)


@router.get("/configs")
async def list_configs(user=Depends(get_current_user)):
    """List every bot_config the user owns — the default plus any per-account
    overrides. Used by the UI's account selector.
    """
    db = get_db()
    cursor = db.bot_configs.find({"user_id": user["id"]})
    docs = await cursor.to_list(length=100)
    return [_serialize(d) for d in docs]


@router.put("/config")
async def update_config(payload: BotConfigUpdate,
                        account_id: Optional[str] = None,
                        user=Depends(get_current_user)):
    db = get_db()
    if account_id:
        owns = await db.accounts.find_one(
            {"_id": ObjectId(account_id), "user_id": user["id"]}
        )
        if not owns:
            raise HTTPException(status_code=404, detail="Account not found")
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
    if "max_lot_size" in update:
        update["max_lot_size"] = max(0.0, float(update["max_lot_size"]))
    update["updated_at"] = datetime.now(timezone.utc).isoformat()

    # Ensure the target doc exists, then PATCH.
    await _get_or_create_config(db, user["id"], account_id)
    await db.bot_configs.update_one(
        _config_filter(user["id"], account_id), {"$set": update}
    )
    cfg = await db.bot_configs.find_one(_config_filter(user["id"], account_id))
    return _serialize(cfg)


@router.delete("/config")
async def reset_account_config(account_id: str, user=Depends(get_current_user)):
    """Remove a per-account override so the account reverts to the user's
    default bot_config. Refuses if account_id is missing.
    """
    db = get_db()
    res = await db.bot_configs.delete_one(
        {"user_id": user["id"], "account_id": account_id}
    )
    return {"deleted": res.deleted_count > 0}


@router.post("/start")
async def start_bot(account_id: Optional[str] = None, user=Depends(get_current_user)):
    db = get_db()
    if account_id:
        owns = await db.accounts.find_one(
            {"_id": ObjectId(account_id), "user_id": user["id"]}
        )
        if not owns:
            raise HTTPException(status_code=404, detail="Account not found")
    await _get_or_create_config(db, user["id"], account_id)
    await db.bot_configs.update_one(
        _config_filter(user["id"], account_id),
        {"$set": {"active": True, "updated_at": datetime.now(timezone.utc).isoformat()}},
    )
    return {"active": True, "account_id": account_id}


@router.post("/stop")
async def stop_bot(account_id: Optional[str] = None, user=Depends(get_current_user)):
    db = get_db()
    await db.bot_configs.update_one(
        _config_filter(user["id"], account_id),
        {"$set": {"active": False, "updated_at": datetime.now(timezone.utc).isoformat()}},
    )
    return {"active": False, "account_id": account_id}


@router.get("/presets")
async def get_strategy_presets(user=Depends(get_current_user)):
    """Return built-in presets + the user's own saved presets, in one payload."""
    custom = await list_user_presets(user["id"])
    return {"presets": list_presets(), "custom": custom}


@router.post("/preset/{key}")
async def apply_strategy_preset(key: str, account_id: Optional[str] = None,
                                user=Depends(get_current_user)):
    """Overlay a named preset (built-in or user-owned) onto the user's bot_config.

    Only the fields defined in the preset are touched. risk_level, symbols,
    drawdown limits, and per-symbol caps are preserved. Pass `?account_id=X`
    to target a per-account override (default scope otherwise).
    """
    db = get_db()
    if account_id:
        owns = await db.accounts.find_one(
            {"_id": ObjectId(account_id), "user_id": user["id"]}
        )
        if not owns:
            raise HTTPException(status_code=404, detail="Account not found")
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

    await _get_or_create_config(db, user["id"], account_id)
    overlay = {**overlay_config, "active_preset": key,
               "updated_at": datetime.now(timezone.utc).isoformat()}
    await db.bot_configs.update_one(
        _config_filter(user["id"], account_id), {"$set": overlay}
    )
    cfg = await db.bot_configs.find_one(_config_filter(user["id"], account_id))
    return {"applied": key, "label": preset_label, "config": _serialize(cfg)}


@router.post("/my-presets")
async def save_user_preset(payload: dict, account_id: Optional[str] = None,
                           user=Depends(get_current_user)):
    """Save the active bot_config's behaviour knobs as a named preset.

    Body: { name, description? }
    Pass `?account_id=X` to snapshot a per-account config; omitted = default.
    """
    db = get_db()
    cfg = await _get_or_create_config(db, user["id"], account_id)
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


@router.get("/health-score")
async def bot_health_score(user=Depends(get_current_user)):
    """Single 0-100 score summarising "is the bot actually working right now?".

    Replaces the cognitive load of scanning seven dashboard strips. Each
    sub-check contributes a weighted deduction; the result + a list of
    actionable issues drives the friendly Bot Health Score widget.

    Issue severity:
      • error   — bot is structurally broken (red)
      • warning — degraded but operating (amber)
      • info    — minor / nice-to-have (grey)
    """
    db = get_db()
    now = datetime.now(timezone.utc)
    issues: list[dict] = []
    score = 100.0

    # --- 1. Account connectivity (max -40) -------------------------------
    accs = await db.accounts.find({
        "user_id": user["id"],
        "$or": [{"mode": "live"}, {"mode": {"$exists": False}}],
    }).to_list(length=20)
    connected = [a for a in accs if a.get("status") == "connected"]
    if not accs:
        score -= 40
        issues.append({"severity": "error", "code": "no_accounts",
                       "label": "No live accounts connected",
                       "fix": "Go to Accounts → Connect MT5 to link a broker."})
    elif not connected:
        score -= 35
        issues.append({"severity": "error", "code": "no_connected_account",
                       "label": "No accounts currently online",
                       "fix": "Restart MetaTrader 5 and attach the STOIC EA to a chart."})

    # --- 2. EA heartbeat freshness (max -20) -----------------------------
    stale_accounts = []
    for a in connected:
        hb = a.get("last_heartbeat")
        if not hb:
            stale_accounts.append(a.get("label"))
            continue
        try:
            dt = datetime.fromisoformat(str(hb).replace("Z", "+00:00"))
            age = (now - dt).total_seconds()
            if age > 90:
                stale_accounts.append(a.get("label"))
        except Exception:
            stale_accounts.append(a.get("label"))
    if stale_accounts:
        score -= min(20, 10 * len(stale_accounts))
        issues.append({"severity": "warning", "code": "stale_heartbeat",
                       "label": f"EA heartbeat stale on {len(stale_accounts)} account(s)",
                       "fix": f"Check {', '.join(stale_accounts)} in MT5 — the EA may have detached.",
                       "details": stale_accounts})

    # --- 3. EA version currency (max -10) --------------------------------
    LATEST_EA = "1.26"
    outdated = [a.get("label") for a in connected
                if (a.get("ea_version") or "") < LATEST_EA]
    if outdated:
        score -= min(10, 5 * len(outdated))
        issues.append({"severity": "warning", "code": "ea_outdated",
                       "label": f"{len(outdated)} terminal(s) on outdated EA",
                       "fix": f"Recompile EmergentTradingBridge.mq5 in MetaEditor (F7) for {', '.join(outdated)}.",
                       "details": outdated})

    # --- 4. Stuck pending modifications (max -15) ------------------------
    # AUTO-CLEAN: any pending_modification queued >10min ago without an EA
    # ack is stale — the EA either ignored it or the queue is broken. Either
    # way, repeatedly penalising the score for it is noise. Clear, then count
    # only the genuinely-recent ones.
    ten_min_ago_iso = (now - timedelta(minutes=10)).isoformat()
    await db.trades.update_many({
        "user_id": user["id"], "status": "open",
        "pending_modification": {"$ne": None},
        "$or": [
            {"pending_modification.requested_at": {"$lt": ten_min_ago_iso}},
            {"pending_modification.requested_at": {"$exists": False}},
        ],
    }, {"$set": {"pending_modification": None,
                  "pending_modification_expired": True}})

    five_min_ago_iso = (now - timedelta(minutes=5)).isoformat()
    stuck = await db.trades.count_documents({
        "user_id": user["id"], "status": "open",
        "pending_modification": {"$ne": None},
        "pending_modification.requested_at": {"$lt": five_min_ago_iso},
    })
    if stuck > 0:
        score -= min(15, 5 * stuck)
        issues.append({"severity": "warning", "code": "stuck_modifications",
                       "label": f"{stuck} trade modification(s) waiting >5min for EA",
                       "fix": "Recompile EA to v1.26 (F7 in MetaEditor) — the modification queue isn't being consumed."})

    # --- 5. Ghost trades — closed with no exit_price (max -10) ----------
    # Acknowledged ghosts (panic / account_deleted / reconciler-only closes
    # that will never have a broker exit_price) don't count — penalising the
    # user for unrecoverable history is just noise.
    ghosts = await db.trades.count_documents({
        "user_id": user["id"], "status": "closed", "exit_price": None,
        "ghost_acknowledged": {"$ne": True},
    })
    if ghosts > 0:
        score -= min(10, 2 * ghosts)
        issues.append({"severity": "info", "code": "ghost_trades",
                       "label": f"{ghosts} closed trade(s) missing exit price",
                       "fix": "EA v1.26 history sweep will auto-fill these within ~60s of connecting."})

    # --- 6. Bot active flag (max -10) ------------------------------------
    cfg = await db.bot_configs.find_one({"user_id": user["id"], "account_id": None})
    any_active = bool(cfg and cfg.get("active"))
    if not any_active:
        per_acc_active = await db.bot_configs.count_documents({
            "user_id": user["id"], "account_id": {"$ne": None}, "active": True,
        })
        any_active = per_acc_active > 0
    if not any_active:
        score -= 10
        issues.append({"severity": "info", "code": "bot_inactive",
                       "label": "Bot is paused (not generating signals)",
                       "fix": "Go to BotConfig and toggle the bot ON to start trading."})

    # --- Final score & summary -------------------------------------------
    score = max(0, min(100, int(round(score))))
    if score >= 90:
        status = "excellent"
        headline = "All systems nominal — the bot is in control."
    elif score >= 75:
        status = "good"
        headline = "Bot is running with minor advisories."
    elif score >= 50:
        status = "degraded"
        headline = "Bot is partially operational — fix the issues below to restore full control."
    else:
        status = "critical"
        headline = "Bot is in trouble — needs attention now."

    return {
        "score": score,
        "status": status,
        "headline": headline,
        "issues": issues,
        "checked_at": now.isoformat(),
        "context": {
            "accounts_connected": len(connected),
            "accounts_total": len(accs),
            "ea_latest_version": LATEST_EA,
        },
    }


@router.get("/quick-actions")
async def quick_actions(user=Depends(get_current_user)):
    """Compact payload for the Quick Actions sticky bar — pre-aggregated so
    the UI doesn't have to make 4 separate calls."""
    db = get_db()
    user_id = user["id"]

    # Bot active state (any config active counts)
    cfg = await db.bot_configs.find_one({"user_id": user_id, "account_id": None})
    bot_active = bool(cfg and cfg.get("active"))
    if not bot_active:
        bot_active = (await db.bot_configs.count_documents({
            "user_id": user_id, "account_id": {"$ne": None}, "active": True,
        })) > 0

    # Open trades count + sum of running pnl (approx — uses last known price)
    open_count = await db.trades.count_documents({
        "user_id": user_id, "status": "open",
    })

    # Today's realised P&L
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    cursor = db.trades.find({
        "user_id": user_id, "status": "closed",
        "closed_at": {"$gte": today_start.isoformat()},
    })
    todays_pnl = 0.0
    todays_count = 0
    async for t in cursor:
        pnl = t.get("pnl")
        if pnl is not None:
            todays_pnl += float(pnl)
            todays_count += 1

    return {
        "bot_active": bot_active,
        "open_trades": open_count,
        "todays_pnl_usd": round(todays_pnl, 2),
        "todays_closed_count": todays_count,
    }


@router.get("/status")
async def get_bot_status(account_id: Optional[str] = None,
                         user=Depends(get_current_user)):
    """Return rich bot runtime status: last signal, last tick, why-no-trade, next tick ETA.

    Status is scoped to the default config when account_id omitted, or to the
    per-account override when supplied.
    """
    db = get_db()
    cfg = await _get_or_create_config(db, user["id"], account_id)
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
