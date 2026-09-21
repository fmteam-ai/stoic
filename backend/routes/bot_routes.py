from datetime import datetime, timezone, timedelta
import os
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from bson import ObjectId

from auth import get_current_user
from database import get_db
from step_up import require_step_up, audit_event
from models import BotConfigUpdate, BotConfigOut
from route_utils import parse_object_id
from risk import get_profile, compute_lot_for_account
from intelligence_counters import get_window_24h as intel_window_24h
from strategy_presets import list_presets, get_preset

# audit r4 P0 · minimum EA version with command fencing + intent journaling —
# live activation is blocked below this (paper accounts unaffected).
FENCING_MIN_EA = "1.50"
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
        "loss_cooldown_enabled": True,
        "loss_cooldown_minutes": 30,
        "min_final_rr": 0.75,
        "payoff_guard_enabled": True,
        "payoff_guard_max_sl_tp1": 1.2,
        "pre_news_protect_enabled": True,
        "pre_news_protect_minutes": 5,
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
        # iter-65 — Daily profit target with lock/stop semantics
        "daily_profit_target_r": float(cfg.get("daily_profit_target_r") or 0.0),
        "daily_profit_target_action": cfg.get("daily_profit_target_action") or "lock",
        "daily_profit_target_escalate": bool(cfg.get("daily_profit_target_escalate", False)),
        "daily_profit_target_escalate_step_r": float(cfg.get("daily_profit_target_escalate_step_r") or 1.0),
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
        "loss_cooldown_enabled": cfg.get("loss_cooldown_enabled", True),
        "loss_cooldown_minutes": cfg.get("loss_cooldown_minutes", 30),
        "min_final_rr": cfg.get("min_final_rr", 0.75),
        "payoff_guard_enabled": cfg.get("payoff_guard_enabled", True),
        "payoff_guard_max_sl_tp1": cfg.get("payoff_guard_max_sl_tp1", 1.2),
        "pre_news_protect_enabled": cfg.get("pre_news_protect_enabled", True),
        "pre_news_protect_minutes": cfg.get("pre_news_protect_minutes", 5),
        "min_confidence_override": cfg.get("min_confidence_override", 0),
        "operational_mode": cfg.get("operational_mode", "observe"),
        "max_lot_size": float(cfg.get("max_lot_size") or 0.0),
        "active_preset": cfg.get("active_preset"),
        # iter-74 · Adaptive Mode visibility
        "profit_taking_mode": cfg.get("profit_taking_mode", "expected_value"),
        "max_tp_pips_per_symbol": cfg.get("max_tp_pips_per_symbol") or {},
        "adaptive_risk_enabled": bool(cfg.get("adaptive_risk_enabled")),
        "adaptive_risk_window": int(cfg.get("adaptive_risk_window") or 20),
        "auto_preset_enabled": bool(cfg.get("auto_preset_enabled")),
        # iter-39 — Paper Shadow Mode (run pipeline, never execute) +
        # per-account crypto risk cap override.
        "paper_shadow_mode": bool(cfg.get("paper_shadow_mode", False)),
        "crypto_risk_pct_per_trade": cfg.get("crypto_risk_pct_per_trade"),
        # iter-41 — payoff-ratio repair (Soft-Stop + Let Winners Run)
        "soft_stop_enabled": bool(cfg.get("soft_stop_enabled", False)),
        "soft_stop_loss_fraction": float(cfg.get("soft_stop_loss_fraction") or 0.6),
        "soft_stop_min_minutes": int(cfg.get("soft_stop_min_minutes") or 10),
        "let_winners_run": bool(cfg.get("let_winners_run", False)),
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
            {"_id": parse_object_id(account_id, "Account"), "user_id": user["id"]}
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
    kelly_on = bool(cfg.get("kelly_enabled", False))

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
            kelly_enabled=kelly_on,
        )
        kelly_f = float(sized.get("kelly_f") or 0)
        kelly_cap = float(profile.get("kelly_cap") or 0)
        absolute_lot = float(sized["lot_size"])
        if kelly_on and max_lot_cap > 0 and kelly_cap > 0:
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
            {"_id": parse_object_id(account_id, "Account"), "user_id": user["id"]}
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


@router.get("/mtf-confluence")
async def mtf_confluence_report(symbol: str = "XAUUSD", user=Depends(get_current_user)):
    """Live MTF cascade report: 4H trend / 1H structure / M15 setup / entry."""
    from market import get_quote
    from mtf_intraday import fetch_mtf_confluence
    try:
        quote = await get_quote(symbol)
        live = float(quote.get("price") or 0)
    except Exception:
        live = 0.0
    report = await fetch_mtf_confluence(symbol, live)
    if not report:
        return {"available": False, "symbol": symbol,
                "note": "M15 stream missing or stale (EA offline?)"}
    return {"available": True, "symbol": symbol, "live_price": live, **report}


@router.get("/pulse")
async def get_bot_pulse(user=Depends(get_current_user)):
    """Return the latest cycle verdict for every bot_config the user owns.

    Lets the UI explain *why* an enabled bot is currently silent (cooldown,
    macro freeze, anti-tilt, AI HOLD, daily cap, etc) instead of leaving the
    trader staring at an empty trades table.

    Shape per pulse:
        {
          "config_id": str, "account_id": str|None, "label": str,
          "active": bool, "paper_shadow_mode": bool, "symbols": [str],
          "pulse": { "ts", "symbol", "action", "reason",
                     "level", "next_eligible_at" } | None,
          "stale_seconds": int|None,   # age of last pulse, None if never recorded
        }
    """
    db = get_db()
    cursor = db.bot_configs.find({"user_id": user["id"]})
    docs = await cursor.to_list(length=100)
    # Batch-resolve custom preset labels so we don't fire one query per config
    # when a user has 20+ accounts, each with a `custom:<id>` active preset.
    custom_ids: set[str] = set()
    for d in docs:
        ap = d.get("active_preset") or ""
        if isinstance(ap, str) and ap.startswith("custom:"):
            try:
                custom_ids.add(ap.split(":", 1)[1])
            except Exception:
                pass
    custom_label_by_id: dict[str, str] = {}
    if custom_ids:
        oids = []
        for pid in custom_ids:
            try:
                oids.append(ObjectId(pid))
            except Exception:
                pass
        if oids:
            cur = db.user_presets.find(
                {"_id": {"$in": oids}, "user_id": user["id"]},
                {"name": 1},
            )
            async for row in cur:
                custom_label_by_id[str(row["_id"])] = row.get("name") or "Custom"
    out: list[dict] = []
    now = datetime.now(timezone.utc)
    for d in docs:
        pulse = d.get("_last_pulse") or None
        stale = None
        if pulse and pulse.get("ts"):
            try:
                ts = datetime.fromisoformat(str(pulse["ts"]).replace("Z", "+00:00"))
                stale = int((now - ts).total_seconds())
            except Exception:
                stale = None
        notable = d.get("_last_notable_pulse") or None
        notable_stale = None
        if notable and notable.get("ts"):
            try:
                nts = datetime.fromisoformat(str(notable["ts"]).replace("Z", "+00:00"))
                notable_stale = int((now - nts).total_seconds())
            except Exception:
                notable_stale = None
        # Friendly label for the UI — account-scoped or "Default"
        acct_id = d.get("account_id")
        label = "Default profile"
        if acct_id:
            try:
                acct = await db.accounts.find_one(
                    {"_id": ObjectId(acct_id), "user_id": user["id"]}
                )
                if acct:
                    label = acct.get("login") or acct.get("broker") or f"Account {acct_id[:6]}"
            except Exception:
                pass
        # Strategy preset label — resolved once, sent alongside the raw key so
        # the UI doesn't need its own preset dictionary.
        active_preset = d.get("active_preset") or ""
        strategy_label = None
        if active_preset:
            if isinstance(active_preset, str) and active_preset.startswith("custom:"):
                pid = active_preset.split(":", 1)[1]
                strategy_label = custom_label_by_id.get(pid, "Custom preset")
            else:
                p = get_preset(active_preset)
                strategy_label = (p or {}).get("label") or active_preset.title()
        out.append({
            "config_id": str(d["_id"]),
            "account_id": acct_id,
            "unbound": acct_id is None,
            "label": label,
            "active": bool(d.get("active")),
            "paper_shadow_mode": bool(d.get("paper_shadow_mode")),
            "symbols": d.get("symbols") or [],
            "strategy_key": active_preset or None,
            "strategy_label": strategy_label,
            "pulse": pulse,
            "stale_seconds": stale,
            "notable": notable,
            "notable_stale_seconds": notable_stale,
        })
    # Sort: active first, then shadow, then inactive
    out.sort(key=lambda x: (not x["active"], not x["paper_shadow_mode"], x["label"]))
    return {"items": out, "loop_interval_sec": int(os.environ.get("BOT_LOOP_INTERVAL_SEC", "60"))}


@router.get("/cooldowns")
async def get_cooldowns(user=Depends(get_current_user)):
    """Per-account cooldown + loss-streak snapshot for the Cooldown UX widget.

    Surfaces the data a trader needs to understand *why* a specific account is
    silent: how long until the next signal eval, how many consecutive losses
    have accumulated against the anti-tilt threshold, and what the last trade
    on each account closed at.

    One card per (user, bot_config) so per-account overrides each get their
    own tile.
    """
    from microstructure import is_market_closed

    db = get_db()
    now = datetime.now(timezone.utc)
    cfgs = await db.bot_configs.find({"user_id": user["id"]}).to_list(length=100)
    out: list[dict] = []
    for cfg in cfgs:
        acct_id = cfg.get("account_id")
        # Account metadata (label, broker)
        label = "Default profile"
        broker = "—"
        if acct_id:
            try:
                acct = await db.accounts.find_one(
                    {"_id": ObjectId(acct_id), "user_id": user["id"]}
                )
                if acct:
                    label = acct.get("label") or acct.get("login") or acct.get("broker") or f"Account {acct_id[:6]}"
                    broker = acct.get("broker") or "—"
            except Exception:
                pass

        cooldown_min = int(cfg.get("signal_cooldown_minutes", 3) or 3)
        symbols = cfg.get("symbols") or []
        pulse = cfg.get("_last_pulse") or {}
        last_eval_ts = pulse.get("ts")
        seconds_since_eval = None
        cooldown_seconds_left = None
        if last_eval_ts:
            try:
                ts = datetime.fromisoformat(str(last_eval_ts).replace("Z", "+00:00"))
                seconds_since_eval = int((now - ts).total_seconds())
                cooldown_seconds_left = max(0, cooldown_min * 60 - seconds_since_eval)
            except Exception:
                pass

        # Per-symbol market closure (so the UI can show "market closed instead
        # of cooldown" when XAUUSD is in the Fri 21 → Sun 22 UTC window).
        symbol_states = []
        for sym in symbols:
            closure = is_market_closed(sym)
            symbol_states.append({
                "symbol": sym,
                "market_closed": closure is not None,
                "market_closure_reason": closure["reason"] if closure else None,
                "reopens_in_hours": closure["reopens_in_hours"] if closure else None,
            })

        # Loss streak — consecutive closed losing trades on this account.
        anti_tilt_n = int(cfg.get("anti_tilt_consecutive_losses", 3) or 0)
        anti_tilt_hours = int(cfg.get("anti_tilt_freeze_hours", 4) or 0)
        anti_tilt_enabled = bool(cfg.get("anti_tilt_enabled", True))

        trade_q: dict = {"user_id": user["id"], "status": "closed"}
        if acct_id:
            trade_q["account_id"] = acct_id
        recent = await db.trades.find(trade_q).sort("closed_at", -1).limit(
            max(anti_tilt_n, 5)
        ).to_list(length=max(anti_tilt_n, 5))

        loss_streak = 0
        win_streak = 0
        for t in recent:
            pnl = float(t.get("pnl") or 0)
            if pnl < 0 and win_streak == 0:
                loss_streak += 1
            elif pnl > 0 and loss_streak == 0:
                win_streak += 1
            else:
                break

        # Anti-tilt status — freeze active iff last N trades all lost AND most
        # recent loss is within the freeze window.
        anti_tilt_active = False
        freeze_unfreeze_in_seconds = None
        if anti_tilt_enabled and anti_tilt_n > 0 and loss_streak >= anti_tilt_n:
            last_close = recent[0].get("closed_at") if recent else None
            if last_close:
                try:
                    lc = datetime.fromisoformat(str(last_close).replace("Z", "+00:00"))
                    elapsed = (now - lc).total_seconds()
                    if elapsed < anti_tilt_hours * 3600:
                        anti_tilt_active = True
                        freeze_unfreeze_in_seconds = int(anti_tilt_hours * 3600 - elapsed)
                except Exception:
                    pass

        # Last closed trade summary (any symbol on this account)
        last_trade = None
        if recent:
            lt = recent[0]
            last_trade = {
                "symbol": lt.get("symbol"),
                "action": lt.get("action"),
                "pnl": float(lt.get("pnl") or 0),
                "closed_at": lt.get("closed_at"),
            }

        out.append({
            "config_id": str(cfg["_id"]),
            "account_id": acct_id,
            "label": label,
            "broker": broker,
            "active": bool(cfg.get("active")),
            "paper_shadow_mode": bool(cfg.get("paper_shadow_mode")),
            "cooldown_minutes": cooldown_min,
            "cooldown_seconds_left": cooldown_seconds_left,
            "seconds_since_eval": seconds_since_eval,
            "symbols": symbol_states,
            "loss_streak": loss_streak,
            "win_streak": win_streak,
            "anti_tilt_threshold": anti_tilt_n,
            "anti_tilt_enabled": anti_tilt_enabled,
            "anti_tilt_active": anti_tilt_active,
            "anti_tilt_unfreeze_in_seconds": freeze_unfreeze_in_seconds,
            "last_trade": last_trade,
        })
    # Active accounts first, then by label
    out.sort(key=lambda x: (not x["active"], x["label"]))
    return {"items": out}


@router.get("/risk-gauge")
async def get_risk_gauge(user=Depends(get_current_user)):
    """Per-account P&L vs circuit-breaker limit, for the dashboard risk gauge.

    Renders how close each account is to tripping its daily / weekly drawdown
    breaker. Uses the same check_and_trip() logic as the live circuit breaker
    (in dry-run mode — does NOT actually trip anything).

    Multi-account isolated: per-account cfg + per-account equity + per-account
    realised P&L. A loss on broker A never inflates broker B's gauge.
    """
    from circuit_breakers import (
        _daily_limit, _weekly_limit, realised_pnl_since, today_iso, week_ago_iso,
    )
    from profit_target import evaluate_profit_target, locked_profit_amount

    db = get_db()
    cfgs = await db.bot_configs.find({"user_id": user["id"]}).to_list(length=100)
    out: list[dict] = []
    for cfg in cfgs:
        acct_id = cfg.get("account_id")
        # Account metadata + equity
        equity = 0.0
        label = "Default profile"
        broker = "—"
        if acct_id:
            try:
                acct = await db.accounts.find_one(
                    {"_id": ObjectId(acct_id), "user_id": user["id"]}
                )
                if acct:
                    label = acct.get("label") or acct.get("login") or acct.get("broker") or f"Account {acct_id[:6]}"
                    broker = acct.get("broker") or "—"
                    equity = float(acct.get("equity") or acct.get("balance") or 0)
            except Exception:
                pass
        else:
            # Default profile sums equity across all the user's accounts
            accts = await db.accounts.find({"user_id": user["id"]}).to_list(length=50)
            equity = sum(float(a.get("equity") or a.get("balance") or 0) for a in accts)

        pnl_today = await realised_pnl_since(db, user["id"], today_iso(), account_id=acct_id)
        pnl_week = await realised_pnl_since(db, user["id"], week_ago_iso(), account_id=acct_id)
        daily_limit_pct = _daily_limit(cfg)
        weekly_limit_pct = _weekly_limit(cfg)

        # Profit target evaluation — surfaces upside mirror of drawdown.
        # Pass the same accounts list so the helper can compute equity.
        scoped_accounts = (
            [a for a in (await db.accounts.find({"user_id": user["id"]}).to_list(50))
             if not acct_id or str(a.get("_id")) == acct_id]
        )
        pt = await evaluate_profit_target(db, user["id"], cfg, scoped_accounts)
        locked_amount = locked_profit_amount(cfg)

        # Convert limits to $ amounts so the UI can show "‑$47 of ‑$120 cap"
        daily_limit_amount = -(daily_limit_pct / 100.0) * equity if equity > 0 else 0
        weekly_limit_amount = -(weekly_limit_pct / 100.0) * equity if equity > 0 else 0

        # % of limit consumed (0 → 100). PnL > 0 → 0% consumed.
        # Negative PnL closer to limit → 100% consumed.
        def _pct_consumed(pnl, limit_amount):
            if limit_amount >= 0 or pnl >= 0:
                return 0.0
            return min(100.0, round((pnl / limit_amount) * 100, 1))

        daily_consumed_pct = _pct_consumed(pnl_today, daily_limit_amount)
        weekly_consumed_pct = _pct_consumed(pnl_week, weekly_limit_amount)

        out.append({
            "config_id": str(cfg["_id"]),
            "account_id": acct_id,
            "label": label,
            "broker": broker,
            "active": bool(cfg.get("active")),
            "tripped": bool(cfg.get("tripped_at")),
            "tripped_reason": cfg.get("tripped_reason"),
            "tripped_kind": cfg.get("tripped_kind"),
            "equity": round(equity, 2),
            "daily": {
                "enabled": bool(cfg.get("daily_drawdown_enabled", True)),
                "pnl": round(pnl_today, 2),
                "limit_pct": daily_limit_pct,
                "limit_amount": round(daily_limit_amount, 2),
                "consumed_pct": daily_consumed_pct,
            },
            "weekly": {
                "enabled": bool(cfg.get("weekly_drawdown_enabled", True)),
                "pnl": round(pnl_week, 2),
                "limit_pct": weekly_limit_pct,
                "limit_amount": round(weekly_limit_amount, 2),
                "consumed_pct": weekly_consumed_pct,
            },
            "profit_target": {
                "enabled": pt["enabled"],
                "mode": pt["mode"],
                "target_r": pt["target_r"],
                "target_amount": pt["target_amount"],
                "current_pnl": pt["current_pnl"],
                "r_dollar_value": pt["r_dollar_value"],
                "hit": pt["hit"],
                "locked_amount": locked_amount,
                # 0–100% progress toward target (0 if disabled or no profit yet)
                "progress_pct": (
                    min(100, round((pt["current_pnl"] / pt["target_amount"]) * 100, 1))
                    if pt["enabled"] and pt["target_amount"] > 0 and pt["current_pnl"] > 0
                    else 0
                ),
            },
        })
    out.sort(key=lambda x: (not x["active"], x["label"]))
    return {"items": out}


RISK_LEVEL_RANK = {"low": 0, "middle": 1, "high": 2, "extreme": 3}
RISK_RAISE_FIELDS = ("adaptive_risk_cap_pct", "crypto_risk_pct_per_trade")


async def _live_context(db, user_id: str, account_id: Optional[str],
                        owns=None) -> bool:
    """True when the action can touch live capital."""
    if account_id:
        acc = owns or await db.accounts.find_one(
            {"_id": parse_object_id(account_id, "Account"), "user_id": user_id})
        return bool(acc) and str(acc.get("mode") or "live").lower() == "live"
    # Dormant/disconnected LIVE accounts still count — they can reconnect
    # at any moment, so the step-up gate must not silently disengage.
    return await db.accounts.count_documents(
        {"user_id": user_id, "status": {"$ne": "deleted"},
         "mode": {"$nin": ["paper"]}}) > 0


def _is_risk_raise(update: dict, current: dict) -> bool:
    cur = current or {}
    if "risk_level" in update:
        if (RISK_LEVEL_RANK.get(str(update["risk_level"] or "").lower(), 0)
                > RISK_LEVEL_RANK.get(str(cur.get("risk_level") or "low").lower(), 0)):
            return True
    for f in RISK_RAISE_FIELDS:
        if update.get(f) is not None:
            try:
                if float(update[f]) > float(cur.get(f) or 0):
                    return True
            except (TypeError, ValueError):
                continue
    if update.get("max_lot_size") is not None:
        try:
            new_v, cur_v = float(update["max_lot_size"]), float(cur.get("max_lot_size") or 0)
            # 0 = uncapped → removing an existing cap or raising it is a raise.
            if cur_v > 0 and (new_v == 0 or new_v > cur_v):
                return True
        except (TypeError, ValueError):
            pass
    return False


@router.put("/config")
async def update_config(payload: BotConfigUpdate,
                        request: Request,
                        account_id: Optional[str] = None,
                        user=Depends(get_current_user)):
    db = get_db()
    owns = None
    if account_id:
        owns = await db.accounts.find_one(
            {"_id": parse_object_id(account_id, "Account"), "user_id": user["id"]}
        )
        if not owns:
            raise HTTPException(status_code=404, detail="Account not found")
    # PATCH semantics — only fields explicitly sent by the client are written.
    # Lets the UI / API do partial updates without nuking other settings.
    update = payload.model_dump(exclude_unset=True)
    if update.get("active") is True and owns is not None:
        problems = await _activation_readiness(db, owns)
        if problems:
            raise HTTPException(status_code=409, detail={
                "code": "activation_not_ready",
                "message": "Live activation blocked — fix these first:",
                "problems": problems})

    # Step-up MFA — activating live trading or raising risk on a live
    # context requires a fresh TOTP verification (iter-153).
    current_cfg = await db.bot_configs.find_one(_config_filter(user["id"], account_id))
    wants_activation = update.get("active") is True and not (current_cfg or {}).get("active")
    risk_raise = _is_risk_raise(update, current_cfg)
    if (wants_activation or risk_raise) and await _live_context(db, user["id"], account_id, owns):
        action = "live_activation" if wants_activation else "risk_raise"
        await require_step_up(db, user, request, action)
        await audit_event(db, user["id"], action,
                          {"account_id": account_id, "via": "config_update",
                           "fields": sorted(update.keys())},
                          request, step_up=True)

    # Operational-mode promotion gate — moving TOWARD live execution is
    # explicit, step-up-MFA'd, certification-tied and audited. Demotions
    # (toward observe/defensive) apply instantly and are audited only.
    if "operational_mode" in update:
        from change_governance import MODE_RANK, snapshot_config
        from operational_modes import DEFAULT_MODE, promotion_gate
        cur_mode = (current_cfg or {}).get("operational_mode") or DEFAULT_MODE
        new_mode = update["operational_mode"]
        if MODE_RANK.get(new_mode, 0) > MODE_RANK.get(cur_mode, 0):
            from entitlements import enforce_mode_ceiling
            await enforce_mode_ceiling(user, new_mode)
            await require_step_up(db, user, request, "live_activation")
            gate = await promotion_gate(db, user["id"], account_id, new_mode)
            if gate["blockers"]:
                await audit_event(db, user["id"], "mode_promotion_blocked",
                                  {"from": cur_mode, "to": new_mode,
                                   "blockers": gate["blockers"]},
                                  request, step_up=True)
                raise HTTPException(status_code=409, detail={
                    "code": "mode_promotion_blocked",
                    "message": "Mode promotion blocked by certification gate:",
                    "blockers": gate["blockers"],
                    "warnings": gate["warnings"]})
            version_id = await snapshot_config(
                db, user["id"], account_id, f"pre-promotion:{new_mode}")
            if new_mode == "autonomous_live":
                update["mode_explicitly_promoted"] = True
            now_dt = datetime.now(timezone.utc)
            await db.governed_changes.insert_one({
                "user_id": user["id"], "account_id": account_id,
                "field": "operational_mode", "old_value": cur_mode,
                "new_value": new_mode, "classification": "aggressive",
                "status": "approved", "source": "mode_promotion",
                "evidence": gate["evidence"],
                "config_version_before": version_id,
                "proposed_at": now_dt, "applied_at": now_dt})
            await audit_event(db, user["id"], "mode_promotion",
                              {"from": cur_mode, "to": new_mode,
                               "warnings": gate["warnings"],
                               "config_version": version_id,
                               "evidence": gate["evidence"]},
                              request, step_up=True)
        elif new_mode != cur_mode:
            await audit_event(db, user["id"], "mode_demotion",
                              {"from": cur_mode, "to": new_mode,
                               "account_id": account_id}, request)

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
    # iter-65: Coerce bogus profit-target-action values at write time so the
    # DB never carries garbage like "TURBO_LOCK". The runtime evaluator also
    # coerces, but cleaning at the boundary keeps stored state honest.
    if "daily_profit_target_action" in update:
        v = (update["daily_profit_target_action"] or "").lower()
        update["daily_profit_target_action"] = v if v in ("lock", "stop") else "lock"
    if "daily_profit_target_r" in update:
        try:
            update["daily_profit_target_r"] = max(0.0, min(20.0, float(update["daily_profit_target_r"])))
        except (TypeError, ValueError):
            update["daily_profit_target_r"] = 0.0
    if "daily_profit_target_escalate" in update:
        update["daily_profit_target_escalate"] = bool(update["daily_profit_target_escalate"])
    if "daily_profit_target_escalate_step_r" in update:
        try:
            v = float(update["daily_profit_target_escalate_step_r"])
            update["daily_profit_target_escalate_step_r"] = max(0.25, min(10.0, v))
        except (TypeError, ValueError):
            update["daily_profit_target_escalate_step_r"] = 1.0
    # iter-74 — Adaptive Mode field coercion
    if "profit_taking_mode" in update:
        v = (update["profit_taking_mode"] or "").lower()
        update["profit_taking_mode"] = v if v in ("expected_value", "win_rate", "trend_follow") else "expected_value"
    if "max_tp_pips_per_symbol" in update:
        update["max_tp_pips_per_symbol"] = {
            str(k).upper(): max(0.0, float(v))
            for k, v in (update["max_tp_pips_per_symbol"] or {}).items()
        }
    if "adaptive_risk_enabled" in update:
        update["adaptive_risk_enabled"] = bool(update["adaptive_risk_enabled"])
    if "adaptive_risk_window" in update:
        try:
            update["adaptive_risk_window"] = max(5, min(200, int(update["adaptive_risk_window"])))
        except (TypeError, ValueError):
            update["adaptive_risk_window"] = 20
    if "auto_preset_enabled" in update:
        update["auto_preset_enabled"] = bool(update["auto_preset_enabled"])
    update["updated_at"] = datetime.now(timezone.utc).isoformat()

    # Ensure the target doc exists, then PATCH atomically (correction #4:
    # config change + immutable version + pointer + audit as one unit).
    await _get_or_create_config(db, user["id"], account_id)
    from config_promotion import apply_config_change
    await apply_config_change(db, user["id"], account_id, update,
                              label="post-update", source="config_update")
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


async def _activation_readiness(db, account) -> list:
    """Audit E10 — live activation prerequisites. Returns blocking problems;
    empty list = ready. Paper accounts are always ready."""
    if not account or (account.get("mode") or "live").lower() == "paper":
        return []
    problems = []
    if str(account.get("account_role") or "STANDARD").upper() == "PAMM_INVESTOR":
        problems.append("Account role is PAMM_INVESTOR — monitor only; "
                        "Execution Authority is locked and the bot can "
                        "never be activated on investor accounts")
    if (account.get("status") or "").lower() not in ("connected", "ok"):
        problems.append(f"Broker account is not connected "
                        f"(status: {account.get('status') or 'unknown'})")
    hb = account.get("last_heartbeat")
    if not hb:
        problems.append("EA has never sent a heartbeat — attach the STOIC EA "
                        "to a chart and enable AutoTrading")
    else:
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(str(hb))).total_seconds()
            if age > 120:
                problems.append(f"EA heartbeat is stale ({int(age)}s old) — "
                                f"check the terminal is running with "
                                f"AutoTrading enabled")
        except (TypeError, ValueError):
            pass
    if not account.get("ea_version"):
        problems.append("EA version unknown — update to the latest STOIC EA")
    elif str(account.get("ea_version")) < FENCING_MIN_EA:
        # audit r4 P0 · live capital requires the fencing-capable EA:
        # command intent/seq dedupe + durable new-order intent journal.
        problems.append(
            f"EA v{account.get('ea_version')} lacks command fencing and "
            f"intent journaling — update to v{FENCING_MIN_EA}+ before "
            f"live activation")
    if not (account.get("equity") or account.get("balance")):
        problems.append("Account equity is unknown — cannot size trades safely")
    return problems


@router.post("/start")
async def start_bot(request: Request, account_id: Optional[str] = None,
                    user=Depends(get_current_user)):
    db = get_db()
    owns = None
    if account_id:
        owns = await db.accounts.find_one(
            {"_id": parse_object_id(account_id, "Account"), "user_id": user["id"]}
        )
        if not owns:
            raise HTTPException(status_code=404, detail="Account not found")
        problems = await _activation_readiness(db, owns)
        if problems:
            raise HTTPException(status_code=409, detail={
                "code": "activation_not_ready",
                "message": "Live activation blocked — fix these first:",
                "problems": problems})
    # Step-up MFA — starting a bot on a live context (fresh activation or
    # panic/trip release) requires a fresh TOTP verification (iter-153).
    cfg_before = await db.bot_configs.find_one(_config_filter(user["id"], account_id))
    if await _live_context(db, user["id"], account_id, owns):
        # Staged-rollout gate (advisory unless STAGE_ENFORCEMENT=true)
        from routes.validation_routes import live_stage_gate
        stage_block = await live_stage_gate(db)
        if stage_block:
            raise HTTPException(status_code=409, detail={
                "code": "deployment_stage_blocked", "message": stage_block})
        action = ("panic_release" if (cfg_before or {}).get("tripped_at")
                  else "live_activation")
        await require_step_up(db, user, request, action)
        await audit_event(db, user["id"], action,
                          {"account_id": account_id, "via": "bot_start"},
                          request, step_up=True)
    await _get_or_create_config(db, user["id"], account_id)
    # CRITICAL: re-enabling a bot must clear the panic/circuit-breaker trip
    # markers, otherwise the UI keeps showing "PANIC LOCK" forever even
    # though `active=True` (iter-91 bug). The bot health doctor treats any
    # bot_config with `tripped_at` set as tripped regardless of `active`.
    await db.bot_configs.update_one(
        _config_filter(user["id"], account_id),
        {
            "$set": {"active": True, "updated_at": datetime.now(timezone.utc).isoformat()},
            "$unset": {"tripped_at": "", "tripped_reason": ""},
        },
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


@router.get("/adaptive-status")
async def adaptive_status(account_id: Optional[str] = None,
                          user=Depends(get_current_user)):
    """iter-74 · Live snapshot of the Win-Rate Adaptive Mode subsystem.

    Returns the current adaptive risk multiplier (based on rolling win
    rate), the regime-auto-selected preset (if enabled), and the user's
    profit-taking mode + TP cap settings. Designed for the dashboard's
    "Adaptive Mode" card.
    """
    from adaptive_mode import compute_risk_multiplier, pick_preset_for_regime
    db = get_db()
    cfg_q = {"user_id": user["id"]}
    cfg_q["account_id"] = account_id if account_id else None
    cfg = await db.bot_configs.find_one(cfg_q) or {}

    # Most-recent signal so we can show which preset auto would pick now
    last_sig_q = {"user_id": user["id"]}
    last_sig = await db.signals.find_one(last_sig_q, sort=[("created_at", -1)])
    regime_exec = None
    regime = None
    if last_sig:
        regime_exec = (last_sig.get("regime_execution_mode") or {}).get("execution_mode")
        regime = (last_sig.get("regime") or {}).get("regime")

    risk_info = await compute_risk_multiplier(
        user_id=user["id"], account_id=account_id,
        window=int(cfg.get("adaptive_risk_window") or 20),
    )

    auto_pick = pick_preset_for_regime(regime_exec, regime)
    return {
        "profit_taking_mode": cfg.get("profit_taking_mode", "expected_value"),
        "max_tp_pips_per_symbol": cfg.get("max_tp_pips_per_symbol") or {},
        "adaptive_risk": {
            "enabled": bool(cfg.get("adaptive_risk_enabled")),
            **risk_info,
        },
        "auto_preset": {
            "enabled": bool(cfg.get("auto_preset_enabled")),
            "would_select": auto_pick["preset_key"],
            "reason": auto_pick["reason"],
            "regime_execution_mode": regime_exec,
            "regime": regime,
        },
        "active_preset": cfg.get("active_preset"),
        "account_id": account_id,
    }


@router.get("/doctor")
async def bot_doctor(account_id: Optional[str] = None,
                     force_refresh: bool = False,
                     user=Depends(get_current_user)):
    """iter-75 · Bot Doctor self-diagnosis (LITE).

    LLM-powered analysis of the last hour of telemetry. Returns a
    structured diagnosis (status / headline / findings / hypothesis /
    recommendations). No auto-apply — surfaces to the dashboard tile so
    the user stays in the loop on every fix.

    Cached for 5 min per (user_id, account_id). Pass force_refresh=true
    to bust the cache (e.g. after fixing an issue).
    """
    from bot_doctor import diagnose
    db = get_db()
    return await diagnose(db, user_id=user["id"],
                          account_id=account_id, force_refresh=force_refresh)


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
            {"_id": parse_object_id(account_id, "Account"), "user_id": user["id"]}
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
        from errors import api_error
        raise api_error(400, "preset_invalid", str(e), exc=e)
    return preset


@router.delete("/my-presets/{preset_id}")
async def delete_my_preset(preset_id: str, user=Depends(get_current_user)):
    ok = await delete_user_preset(user_id=user["id"], preset_id=preset_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Custom preset not found")
    return {"deleted": True, "id": preset_id}


@router.get("/safety-status")
async def safety_status(user=Depends(get_current_user)):
    """Iter-151 · single Trading-Safety truth for the dashboard banner:
    live mode, unprotected fills, unresolved submissions, stale feeds,
    tripped breakers and worst daily-drawdown usage."""
    db = get_db()
    now = datetime.now(timezone.utc)

    def _age(ts):
        try:
            d = datetime.fromisoformat(str(ts))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return (now - d).total_seconds()
        except Exception:
            return None

    live_accounts, stale_feeds = [], []
    paper_count, max_hb_age = 0, None
    async for a in db.accounts.find(
            {"user_id": user["id"], "status": {"$ne": "deleted"}},
            {"label": 1, "mode": 1, "trading_enabled": 1,
             "last_heartbeat": 1, "dormant": 1}):
        if a.get("dormant") or a.get("trading_enabled") is False:
            continue
        if str(a.get("mode") or "").lower() == "live":
            live_accounts.append(a.get("label"))
        elif str(a.get("mode") or "").lower() == "paper":
            paper_count += 1
        age = _age(a.get("last_heartbeat"))
        if age is not None:
            max_hb_age = age if max_hb_age is None else max(max_hb_age, age)
        if age is None or age > 300:
            stale_feeds.append({"label": a.get("label"),
                                "age_sec": int(age) if age else None})

    if live_accounts and paper_count:
        deployment_mode = "MIXED"
    elif live_accounts:
        deployment_mode = "LIVE"
    elif paper_count:
        deployment_mode = "PAPER"
    else:
        deployment_mode = "IDLE"

    # Capital currently at risk — planned risk of every open position.
    capital_at_risk = 0.0
    async for t in db.trades.find({"user_id": user["id"], "status": "open"},
                                  {"risk_amount": 1}):
        try:
            capital_at_risk += float(t.get("risk_amount") or 0)
        except (TypeError, ValueError):
            pass

    from protection_guard import unprotected_open_query
    unprotected = await db.trades.count_documents(
        unprotected_open_query({"user_id": user["id"]}))
    unresolved = await db.trades.count_documents({
        "user_id": user["id"], "status": "pending",
        "submission_state": "broker_accepted_unresolved"})

    tripped = []
    panic_active = False
    async for c in db.bot_configs.find(
            {"user_id": user["id"], "tripped_at": {"$ne": None}},
            {"tripped_reason": 1, "tripped_kind": 1}):
        tripped.append({"reason": c.get("tripped_reason"),
                        "kind": c.get("tripped_kind")})
        if "PANIC" in str(c.get("tripped_reason") or "").upper() \
                or str(c.get("tripped_kind") or "").lower() == "panic":
            panic_active = True

    daily_worst = 0.0
    try:
        gauge = await get_risk_gauge(user=user)
        for g in gauge.get("items", []):
            daily_worst = max(daily_worst, float(
                (g.get("daily") or {}).get("consumed_pct") or 0))
    except Exception:
        pass

    level = "safe"
    if stale_feeds or daily_worst >= 70 or tripped:
        level = "warn"
    if unprotected > 0 or unresolved > 0 or (tripped and live_accounts):
        level = "critical"

    return {"generated_at": now.isoformat(),
            "level": level,
            "deployment_mode": deployment_mode,
            "capital_at_risk": round(capital_at_risk, 2),
            "reconciliation_delay_sec": (int(max_hb_age)
                                         if max_hb_age is not None else None),
            "panic_active": panic_active,
            "live_accounts": live_accounts,
            "unprotected_open": unprotected,
            "unresolved_submissions": unresolved,
            "stale_feeds": stale_feeds,
            "tripped": tripped,
            "daily_worst_consumed_pct": round(daily_worst, 1)}


@router.get("/execution-health")
async def execution_health(user=Depends(get_current_user)):
    """Iter-147 · execution-plumbing visibility: outbox backlog, worker
    leases, submission slots, open-trade lifecycle/protection states and
    expected-vs-realized execution costs."""
    from monte_carlo import typical_cost
    from pip_utils import base_symbol as _base, price_to_pips
    db = get_db()
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    pending = await db.outbox.count_documents({"state": "pending"})
    failed = await db.outbox.count_documents({"state": "failed"})
    oldest_age = None
    if pending:
        oldest = await db.outbox.find_one({"state": "pending"},
                                          sort=[("created_at", 1)])
        try:
            ts = datetime.fromisoformat(str(oldest["created_at"]))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            oldest_age = max(0, int((now - ts).total_seconds()))
        except Exception:
            pass

    workers = []
    async for w in db.worker_leases.find({}):
        exp = str(w.get("expires_at") or "")
        workers.append({"name": str(w.get("_id")), "holder": w.get("holder"),
                        "expires_at": exp, "alive": exp >= now_iso})

    account_names = {}
    async for a in db.accounts.find({"user_id": user["id"]},
                                    {"account_name": 1, "broker": 1}):
        account_names[str(a["_id"])] = (a.get("account_name")
                                        or a.get("broker")
                                        or str(a["_id"])[-6:])
    owners = []
    if account_names:
        async for o in db.scalp_owners.find(
                {"account_id": {"$in": list(account_names)}}):
            lease_until = str(o.get("lease_until") or "")
            owners.append({"account_id": o.get("account_id"),
                           "account": account_names.get(o.get("account_id")),
                           "worker_id": o.get("worker_id"),
                           "lease_epoch": int(o.get("lease_epoch") or 0),
                           "lease_until": lease_until,
                           "alive": lease_until >= now_iso})

    unresolved = await db.trades.count_documents(
        {"user_id": user["id"], "status": "pending",
         "submission_state": "broker_accepted_unresolved"})

    slots_total = await db.scalp_submission_slots.count_documents({})
    slots_active = await db.scalp_submission_slots.count_documents(
        {"lease_until": {"$gte": now_iso}, "token": {"$ne": None}})

    lifecycle: dict = {}
    protection = {"protected": 0, "unprotected": 0, "no_stop": 0}
    stuck = []
    async for t in db.trades.find(
            {"user_id": user["id"], "status": {"$in": ["open", "pending"]}},
            {"symbol": 1, "status": 1, "stop_loss": 1, "account_id": 1,
             "lifecycle_state": 1, "submission_state": 1,
             "_dispatched_at": 1}).limit(300):
        state = (t.get("lifecycle_state") or t.get("submission_state")
                 or ("OPEN" if t.get("status") == "open" else "QUEUED"))
        state = str(state).upper()
        lifecycle[state] = lifecycle.get(state, 0) + 1
        if t.get("status") == "open":
            if not t.get("stop_loss"):
                protection["no_stop"] += 1
            elif str(t.get("lifecycle_state") or "") in (
                    "FILLED_UNPROTECTED", "PROTECTION_REQUESTED"):
                protection["unprotected"] += 1
            else:
                protection["protected"] += 1
        if t.get("status") == "pending" and t.get("_dispatched_at"):
            try:
                dts = datetime.fromisoformat(str(t["_dispatched_at"]))
                if dts.tzinfo is None:
                    dts = dts.replace(tzinfo=timezone.utc)
                age = (now - dts).total_seconds()
                if age > 180:
                    stuck.append({"trade_id": str(t["_id"]),
                                  "symbol": t.get("symbol"),
                                  "age_sec": int(age), "state": state,
                                  "account": account_names.get(
                                      t.get("account_id"))})
            except Exception:
                pass

    cost_rows: dict = {}
    async for t in db.trades.find(
            {"user_id": user["id"], "status": "closed", "origin": "auto",
             "slippage_pips": {"$exists": True, "$ne": None}},
            {"symbol": 1, "base_symbol": 1, "slippage_pips": 1,
             "entry_price": 1}).sort("closed_at", -1).limit(60):
        sym = t.get("base_symbol") or _base(t.get("symbol"))
        row = cost_rows.setdefault(sym, {"n": 0, "slip": 0.0, "exp": None})
        row["n"] += 1
        row["slip"] += abs(float(t.get("slippage_pips") or 0))
        if row["exp"] is None and t.get("entry_price"):
            try:
                row["exp"] = round(price_to_pips(
                    sym, typical_cost(sym, float(t["entry_price"]))), 2)
            except Exception:
                pass
    costs = [{"symbol": s, "trades": r["n"],
              "avg_realized_slippage_pips": round(r["slip"] / r["n"], 2),
              "expected_cost_pips": r["exp"]}
             for s, r in sorted(cost_rows.items())]

    # dispatch → broker-open latency percentiles (last 100 auto trades)
    lat = []
    async for t in db.trades.find(
            {"user_id": user["id"], "origin": "auto",
             "_dispatched_at": {"$exists": True}, "opened_at": {"$ne": None}},
            {"_dispatched_at": 1, "opened_at": 1}
            ).sort("opened_at", -1).limit(100):
        try:
            d = datetime.fromisoformat(str(t["_dispatched_at"]))
            o = t["opened_at"]
            if not isinstance(o, datetime):
                o = datetime.fromisoformat(str(o))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            if o.tzinfo is None:
                o = o.replace(tzinfo=timezone.utc)
            s = (o - d).total_seconds()
            if 0 <= s < 3600:
                lat.append(s)
        except Exception:
            pass
    lat.sort()

    def _pct(p):
        return (round(lat[min(len(lat) - 1, int(p * len(lat)))], 2)
                if lat else None)

    latency = {"n": len(lat), "p50_sec": _pct(0.50),
               "p95_sec": _pct(0.95), "p99_sec": _pct(0.99)}

    day_ago = (now - timedelta(days=1)).isoformat()
    counters = {
        "rejects_24h": await db.trades.count_documents(
            {"user_id": user["id"], "status": "failed",
             "_dispatched_at": {"$gte": day_ago}}),
        "replays_24h": await db.trades.count_documents(
            {"user_id": user["id"],
             "journal_replayed_at": {"$gte": day_ago}}),
        "partial_fills_open": await db.trades.count_documents(
            {"user_id": user["id"], "status": "open",
             "partial_fill": True}),
        "partial_fill_events_24h": await db.trade_events.count_documents(
            {"event_type": "PartialFillAdopted", "at": {"$gte": day_ago}}),
    }

    import time as _time
    t0 = _time.perf_counter()
    await db.command("ping")
    heartbeats = []
    async for a in db.accounts.find(
            {"user_id": user["id"], "trading_enabled": {"$ne": False},
             "dormant": {"$ne": True}, "status": {"$ne": "deleted"}},
            {"label": 1, "last_heartbeat": 1, "broker_utc_offset_sec": 1}):
        hb_age = None
        try:
            d = datetime.fromisoformat(str(a.get("last_heartbeat")))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            hb_age = int((now - d).total_seconds())
        except Exception:
            pass
        heartbeats.append({"label": a.get("label"), "age_sec": hb_age,
                           "fresh": hb_age is not None and hb_age < 300,
                           "clock_offset_sec": a.get("broker_utc_offset_sec")})

    # candle-feed freshness (worst lag per symbol/timeframe for this user)
    feeds = []
    async for f in db.candle_feed_health.find(
            {"user_id": user["id"]},
            {"symbol": 1, "timeframe": 1, "last_received_at": 1,
             "bar_lag_s": 1, "last_write_ok": 1}).limit(50):
        f_age = None
        try:
            d = datetime.fromisoformat(str(f.get("last_received_at")))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            f_age = int((now - d).total_seconds())
        except Exception:
            pass
        feeds.append({"key": f"{f.get('symbol')}_{f.get('timeframe')}",
                      "age_sec": f_age,
                      "write_ok": bool(f.get("last_write_ok")),
                      "fresh": f_age is not None
                               and f_age < 2 * int(f.get("bar_lag_s") or 900)})
    feeds.sort(key=lambda x: -(x["age_sec"] or 10**9))

    from ws_manager import manager as _wsm
    infra = {"mongo_latency_ms": round((_time.perf_counter() - t0) * 1000, 1),
             "ws_clients": sum(len(s) for s in _wsm._connections.values()),
             "heartbeats": heartbeats,
             "feeds": feeds[:12],
             "feeds_stale": sum(1 for f in feeds if not f["fresh"])}

    return {"generated_at": now_iso,
            "latency": latency,
            "counters": counters,
            "infra": infra,
            "outbox": {"pending": pending, "failed": failed,
                       "oldest_pending_age_sec": oldest_age},
            "workers": workers,
            "account_leases": owners,
            "submission_slots": {"total": slots_total,
                                 "active": slots_active,
                                 "free": max(0, slots_total - slots_active)},
            "lifecycle": lifecycle,
            "protection": protection,
            "stuck_pending": stuck[:10],
            "unresolved_submissions": unresolved,
            "costs": costs}


@router.get("/forecast-status")
async def forecast_status(user=Depends(get_current_user)):
    """Forecast Health card — is the Chronos forecaster actually running?
    Merges this process's live status with the persisted status of every
    other process (worker-trading loads its own model in production)."""
    db = get_db()
    from forecast_agent import runtime_status
    local = runtime_status()
    procs = {local["role"]: local}
    async for d in db.forecast_runtime.find({}, {"_id": 0}):
        role = d.get("role")
        if not role:
            continue
        if role != local["role"]:
            procs[role] = d
        elif not local.get("last_forecast_at"):
            # this process restarted since its last forecast — keep the
            # persisted history visible, but report the live load state.
            for k in ("last_forecast_at", "last_latency_ms", "last_symbol",
                      "last_source", "load_ms"):
                if local.get(k) is None:
                    local[k] = d.get(k)
            local["forecasts_total"] = max(local["forecasts_total"],
                                           d.get("forecasts_total") or 0)
            local["since_restart"] = True
    now = datetime.now(timezone.utc)

    def _age_s(ts):
        if not ts:
            return None
        try:
            return max(0, int((now - datetime.fromisoformat(
                ts.replace("Z", "+00:00"))).total_seconds()))
        except Exception:  # noqa: BLE001
            return None

    rows = []
    for role, p in procs.items():
        rows.append({**p, "last_forecast_age_s": _age_s(p.get("last_forecast_at")),
                     "status_age_s": _age_s(p.get("updated_at"))})
    # the process that actually forecasts is the freshest forecaster
    active = sorted([r for r in rows if r.get("last_forecast_at")],
                    key=lambda r: r["last_forecast_age_s"])
    lead = active[0] if active else next(
        (r for r in rows if r.get("model_loaded")), rows[0])
    if not local["agent_enabled"]:
        state = "disabled"
    elif lead.get("model_failed") and not lead.get("model_loaded"):
        state = "failed" if lead.get("torch_available") else "not_installed"
    elif not any(r.get("ml_runtime_enabled") for r in rows):
        state = "gated_off"
    elif lead.get("last_forecast_at") and lead["last_forecast_age_s"] <= 1800:
        state = "running"
    elif lead.get("model_loaded"):
        state = "idle"
    else:
        state = "not_loaded"
    out = {"state": state, "model": local["model"],
           "cache_ttl_s": local["cache_ttl_s"], "lead": lead,
           "processes": sorted(rows, key=lambda r: r["role"]),
           "checked_at": now.isoformat()}
    if user.get("role") != "admin":
        # audit #5 SEC-001 — ops internals (host:pid, memory budget, raw
        # error strings, process topology) are admin-only; customers get
        # the functional verdict.
        keep = ("model_loaded", "model_failed", "load_ms", "last_forecast_at",
                "last_forecast_age_s", "last_latency_ms", "last_symbol",
                "last_source", "forecasts_total", "cache_entries",
                "cache_hits", "cache_misses", "ml_runtime_enabled",
                "agent_enabled")
        out["lead"] = {k: lead.get(k) for k in keep}
        out["processes"] = []
    return out


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
    # iter-70 · accounts marked dormant by the heartbeat check (below) shouldn't
    # tank the score with the broader "no connected account" penalty — they
    # were online once, the user just closed MT5. Count them in a separate
    # bucket and apply a softer deduction.
    # iter-70b · Auto-clear the dormant flag for accounts that have come back
    # online with a fresh heartbeat — keeps the advisory honest.
    revived_ids: list = []
    for a in accs:
        if not a.get("dormant"):
            continue
        hb = a.get("last_heartbeat")
        if not hb:
            continue
        try:
            dt = datetime.fromisoformat(str(hb).replace("Z", "+00:00"))
            age = (now - dt).total_seconds()
        except Exception:
            continue
        # Fresh heartbeat (< 5 min) AND status is back to connected? Revive.
        if age < 300 and a.get("status") == "connected":
            revived_ids.append(a.get("_id"))
            a["dormant"] = False  # mirror in-memory so the rest of this call sees reality
    if revived_ids:
        await db.accounts.update_many(
            {"_id": {"$in": revived_ids}},
            {"$set": {"dormant": False, "revived_at": now.isoformat()}},
        )
    non_dormant_accs = [a for a in accs if not a.get("dormant")]
    if not accs:
        score -= 40
        issues.append({"severity": "error", "code": "no_accounts",
                       "label": "No live accounts connected",
                       "fix": "Go to Accounts → Connect MT5 to link a broker."})
    elif not connected and non_dormant_accs:
        # User has accounts but none online — and they're not dormant, meaning
        # the user expects them to be online. This IS broken.
        score -= 35
        issues.append({"severity": "error", "code": "no_connected_account",
                       "label": "No accounts currently online",
                       "fix": "Restart MetaTrader 5 and attach the STOIC EA to a chart."})

    # --- 2. EA heartbeat freshness (max -20) -----------------------------
    # Distinguish 3 states:
    #   • fresh           (< 90s)                  → no deduction
    #   • stale           (90s — 1h)               → -5 each (max -10), warning
    #   • dormant         (> 1h, account abandoned) → single -5 advisory,
    #                                                 NOT counted as broken
    # Dormant accounts are flipped to status="disconnected" with `dormant:true`
    # so the dashboard and downstream code see reality.
    DORMANT_AFTER_SEC = 3600  # 1h with no EA ping → user closed MT5, not a bug
    stale_accounts = []
    dormant_account_ids: list = []
    for a in connected:
        hb = a.get("last_heartbeat")
        age: float = float("inf")
        if hb:
            try:
                dt = datetime.fromisoformat(str(hb).replace("Z", "+00:00"))
                age = (now - dt).total_seconds()
            except Exception:
                age = float("inf")
        if age <= 90:
            continue
        if age > DORMANT_AFTER_SEC:
            dormant_account_ids.append(a.get("_id"))
            continue
        stale_accounts.append(a.get("label"))

    # Auto-mark dormant accounts as disconnected so reality reflects state.
    if dormant_account_ids:
        await db.accounts.update_many(
            {"_id": {"$in": dormant_account_ids}},
            {"$set": {"status": "disconnected", "dormant": True}},
        )
        # Refresh `connected` view to match the new reality.
        connected = [a for a in connected if a.get("_id") not in dormant_account_ids]

    if stale_accounts:
        score -= min(10, 5 * len(stale_accounts))
        issues.append({"severity": "warning", "code": "stale_heartbeat",
                       "label": f"EA heartbeat stale on {len(stale_accounts)} account(s)",
                       "fix": f"Check {', '.join(stale_accounts)} in MT5 — the EA may have detached.",
                       "details": stale_accounts})

    if dormant_account_ids:
        score -= 5
        dormant_labels = [str(a.get("label")) for a in accs
                          if a.get("_id") in dormant_account_ids]
        issues.append({"severity": "info", "code": "dormant_accounts",
                       "label": f"{len(dormant_account_ids)} account(s) dormant (offline >1h)",
                       "fix": (f"Auto-marked as disconnected: {', '.join(dormant_labels)}. "
                               "Open MT5 + attach the EA to reactivate, or ignore if "
                               "these are old test accounts."),
                       "details": dormant_labels})
    else:
        # No newly-dormant flip this call, but already-dormant accounts
        # should still be surfaced as an info advisory (no deduction).
        already_dormant = [a for a in accs if a.get("dormant")]
        if already_dormant:
            issues.append({
                "severity": "info", "code": "dormant_accounts",
                "label": f"{len(already_dormant)} account(s) currently dormant",
                "fix": ("Re-attach the EA in MT5 to reactivate, or archive these "
                        "if they're no longer in use."),
                "details": [a.get("label") for a in already_dormant],
            })

    # --- 3. EA version currency (max -10) --------------------------------
    LATEST_EA = "1.56"
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
                       "fix": "Update the EA to v1.40+ (Accounts page → DOWNLOAD EA, recompile with F7) — older builds ignore FULL_CLOSE queue entries."})

    # --- 5. Ghost trades — closed with no exit_price (max -10) ----------
    # Acknowledged ghosts (panic / account_deleted / reconciler-only closes
    # that will never have a broker exit_price) don't count — penalising the
    # user for unrecoverable history is just noise.
    # AUTO-CLEAN: ghosts older than 24h are unrecoverable — the EA history
    # sweep on connection only backfills recent state. Mark them
    # ghost_acknowledged so they stop polluting the score.
    day_ago_iso = (now - timedelta(hours=24)).isoformat()
    await db.trades.update_many({
        "user_id": user["id"], "status": "closed", "exit_price": None,
        "ghost_acknowledged": {"$ne": True},
        "closed_at": {"$lt": day_ago_iso},
    }, {"$set": {"ghost_acknowledged": True, "ghost_auto_ack_reason": "older_than_24h"}})

    # AUTO-CLEAN: ghosts attached to an account that no longer exists are
    # also unrecoverable — the EA on that account is gone (deleted/replaced),
    # so the history sweep can never reach the broker to fill exit_price.
    # Common cause: testing-agent synthetic accounts cleaned up after a run
    # but leaving orphan trade rows behind.
    candidate_orphans = await db.trades.find({
        "user_id": user["id"], "status": "closed", "exit_price": None,
        "ghost_acknowledged": {"$ne": True},
    }, {"_id": 1, "account_id": 1}).to_list(length=200)
    if candidate_orphans:
        acct_ids = {t.get("account_id") for t in candidate_orphans if t.get("account_id")}
        live_oids = set()
        if acct_ids:
            # Resolve which account_ids still exist (account_id is stored as str,
            # accounts._id is ObjectId — convert defensively).
            from bson.errors import InvalidId
            try_oids = []
            for aid in acct_ids:
                try:
                    try_oids.append(ObjectId(aid))
                except (InvalidId, TypeError):
                    pass
            if try_oids:
                live_docs = await db.accounts.find(
                    {"_id": {"$in": try_oids}}, {"_id": 1}
                ).to_list(length=len(try_oids))
                live_oids = {str(d["_id"]) for d in live_docs}
        orphan_ids = [t["_id"] for t in candidate_orphans
                      if t.get("account_id") and t["account_id"] not in live_oids]
        if orphan_ids:
            await db.trades.update_many(
                {"_id": {"$in": orphan_ids}},
                {"$set": {"ghost_acknowledged": True,
                          "ghost_auto_ack_reason": "account_deleted"}},
            )

    ghosts = await db.trades.count_documents({
        "user_id": user["id"], "status": "closed", "exit_price": None,
        "ghost_acknowledged": {"$ne": True},
    })
    if ghosts > 0:
        score -= min(10, 2 * ghosts)
        issues.append({"severity": "info", "code": "ghost_trades",
                       "label": f"{ghosts} closed trade(s) missing exit price",
                       "fix": f"EA v{LATEST_EA} history sweep will auto-fill these within ~60s of connecting."})

    # --- 6. Bot active flag (max -5, advisory) ----------------------------
    # When the user has *explicitly* paused the bot (any config with active=False)
    # this is a deliberate state, not a malfunction. Surface it but with a
    # light advisory deduction so the score doesn't tank just because the
    # user chose to pause.
    cfg = await db.bot_configs.find_one({"user_id": user["id"], "account_id": None})
    any_active = bool(cfg and cfg.get("active"))
    if not any_active:
        per_acc_active = await db.bot_configs.count_documents({
            "user_id": user["id"], "account_id": {"$ne": None}, "active": True,
        })
        any_active = per_acc_active > 0
    if not any_active:
        score -= 5
        issues.append({"severity": "info", "code": "bot_inactive",
                       "label": "Bot is paused (not generating signals)",
                       "fix": "Go to BotConfig and toggle the bot ON to start trading."})

    # --- 6c. Broker-rejecting accounts (iter-71, max -25 critical) ----------
    # Any account auto-halted by the broker-reject circuit breaker — the
    # broker has rejected our last 3+ orders with the same retcode. Surfaces
    # high in the issue list so the user immediately knows the bot can't
    # trade on that account until they intervene.
    blocked_accs = [a for a in accs if a.get("trading_blocked")]
    if blocked_accs:
        score -= min(25, 15 * len(blocked_accs))
        for ba in blocked_accs:
            issues.append({
                "severity": "error",
                "code": "broker_rejecting_trades",
                "label": (f"Broker auto-halted {ba.get('label')}: "
                          f"{ba.get('block_retcode_label') or 'unknown'} "
                          f"({ba.get('block_retcode') or '?'})"),
                "fix": (ba.get("block_hint") or "Check MT5 Experts tab for details.") +
                        "  After fixing, click 'Resume Trading' on this account.",
                "details": {
                    "account_id": str(ba["_id"]),
                    "blocked_at": ba.get("blocked_at"),
                    "reason": ba.get("block_reason"),
                },
            })

    # --- 7. Anti-tilt freeze active (max -5, advisory only) --------------
    # The bot is functioning correctly — anti-tilt is a designed risk halt
    # after N consecutive losses. We surface it explicitly so the user knows
    # *why* no trades are firing, with a small deduction to make sure the
    # widget catches their eye instead of showing a misleading "100/100".
    tilt_frozen_accounts = []
    tilt_cfgs = await db.bot_configs.find({"user_id": user["id"]}).to_list(length=20)
    for tc in tilt_cfgs:
        if not tc.get("anti_tilt_enabled", True):
            continue
        atn = int(tc.get("anti_tilt_consecutive_losses", 3) or 0)
        ath = int(tc.get("anti_tilt_freeze_hours", 4) or 0)
        if atn <= 0 or ath <= 0:
            continue
        at_q = {"user_id": user["id"], "status": "closed"}
        if tc.get("account_id"):
            at_q["account_id"] = tc["account_id"]
        at_recent = await db.trades.find(at_q).sort("closed_at", -1).limit(atn).to_list(length=atn)
        if len(at_recent) == atn and all(float(r.get("pnl") or 0) < 0 for r in at_recent):  # iter-45: strict losses only
            last_close = at_recent[0].get("closed_at")
            try:
                lc = datetime.fromisoformat(str(last_close).replace("Z", "+00:00"))
                unfreeze_at = lc + timedelta(hours=ath)
                remaining = (unfreeze_at - now).total_seconds()
                if remaining > 0:
                    mins_left = int(remaining // 60)
                    hrs, mins = divmod(mins_left, 60)
                    eta = f"{hrs}h {mins}m" if hrs else f"{mins}m"
                    label = tc.get("account_id") or "default"
                    tilt_frozen_accounts.append({"scope": label, "eta": eta,
                                                  "unfreeze_at": unfreeze_at.isoformat()})
            except Exception:
                pass
    if tilt_frozen_accounts:
        score -= 5
        eta_first = tilt_frozen_accounts[0]["eta"]
        issues.append({"severity": "warning", "code": "anti_tilt_freeze",
                       "label": f"Anti-tilt freeze active — auto-execute paused for {eta_first}",
                       "fix": "Designed protection after consecutive losses. Lower freeze window or disable in Bot Config → Capital Preservation if intentional.",
                       "details": tilt_frozen_accounts})

    # --- review P0-3: FAIL-CLOSED HARD CAPS ------------------------------
    # A safety score must never read EXCELLENT while truth/reconciliation
    # is failing. Caps are applied AFTER all deductions.
    hard_caps: list[dict] = []
    try:
        from state_contract import contract as _state_contract
        sc = await _state_contract(db, user["id"])
        rows = [r for r in sc["accounts"] if r.get("account_enabled")]
        bot_rows = [r for r in rows if r.get("bot_enabled")]
        bad_truth = [r for r in bot_rows if r["position_truth"] != "FRESH"]
        if bad_truth:
            hard_caps.append({
                "cap": 25, "code": "position_truth_not_fresh",
                "label": f"{len(bad_truth)} bot account(s) with "
                         f"{bad_truth[0]['position_truth']} position truth "
                         "— broker state is not verified"})
        if any(r["effective_state"] == "PANIC" for r in bot_rows):
            hard_caps.append({"cap": 25, "code": "panic_tripped",
                              "label": "Panic switch is tripped"})
        # synthetic/QA alerts never cap a real health score (review P1-7)
        blocked = [r for r in bot_rows
                   if r["effective_state"] in ("BLOCKED", "DISCONNECTED")]
        if blocked:
            hard_caps.append({
                "cap": 60, "code": "execution_blocked",
                "label": f"{len(blocked)} enabled bot(s) cannot execute "
                         f"({blocked[0]['effective_state']})"})
        reduced = [r for r in bot_rows
                   if r.get("execution_authority") != "FULL"]
        if reduced and not blocked:
            hard_caps.append({
                "cap": 60, "code": "authority_reduced",
                "label": f"{len(reduced)} bot account(s) with reduced "
                         "execution authority"})
    except Exception:  # noqa: BLE001 — caps degrade gracefully, never 500
        pass
    if user.get("role") == "admin":
        try:
            unk = await db.execution_intents.count_documents(
                {"status": "unknown"})
            if unk:
                hard_caps.append({
                    "cap": 25, "code": "reconciliation_pending",
                    "label": f"{unk} execution(s) UNKNOWN — broker "
                             "reconciliation pending"})
            crit = await db.ops_alerts.count_documents(
                {"acked_at": None, "severity": "critical",
                 "synthetic": {"$ne": True}})
            if crit:
                crit_docs = await db.ops_alerts.find(
                    {"acked_at": None, "severity": "critical",
                     "synthetic": {"$ne": True}},
                    {"kind": 1, "message": 1, "last_seen_at": 1,
                     "created_at": 1, "occurrences": 1, "dedup_key": 1}
                ).sort("last_seen_at", -1).to_list(10)
                details = []
                def _utciso(v):
                    if not v:
                        return None
                    if v.tzinfo is None:
                        v = v.replace(tzinfo=timezone.utc)
                    return v.isoformat()
                for d in crit_docs:
                    prior = 0
                    if d.get("dedup_key"):
                        prior = await db.ops_alerts.count_documents(
                            {"dedup_key": d["dedup_key"],
                             "acked_at": {"$ne": None}})
                    details.append({
                        "kind": d.get("kind"),
                        "message": d.get("message"),
                        "occurrences": d.get("occurrences", 1),
                        "prior_acked": prior,
                        "created_at": _utciso(d.get("created_at")),
                        "last_seen_at": _utciso(d.get("last_seen_at")),
                    })
                hard_caps.append({
                    "cap": 45, "code": "critical_alerts_open",
                    "label": f"{crit} unacknowledged CRITICAL platform "
                             "alert(s)",
                    "details": details})
        except Exception:  # noqa: BLE001
            pass
    for c in hard_caps:
        issues.append({
            "severity": "error" if c["cap"] <= 25 else "warning",
            "code": c["code"],
            "label": f"HARD CAP {c['cap']} — {c['label']}",
            "fix": "Fail-closed rule: health cannot read higher while this "
                   "condition is active.",
            **({"details": c["details"]} if c.get("details") else {})})
        if score > c["cap"]:
            score = c["cap"]

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

    from state_contract import inventory as _inv
    inv = await _inv(db, user["id"])
    return {
        "score": score,
        "status": status,
        "headline": headline,
        "issues": issues,
        "hard_caps": hard_caps,
        "checked_at": now.isoformat(),
        "context": {
            # audit v3 P0-2 — canonical inventory counters, never recomputed
            "accounts_connected": inv["ea_connection"]["fresh"]
            + inv["ea_connection"]["paper"],
            "accounts_total": inv["accounts_configured"],
            "accounts_enabled": inv["accounts_enabled"],
            "bots_requested_on": inv["bots_requested_on"],
            "ea_connection": inv["ea_connection"],
            "inventory_as_of": inv["as_of"],
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
    bot_pnl = 0.0
    bot_count = 0
    manual_pnl = 0.0
    async for t in cursor:
        pnl = t.get("pnl")
        if pnl is not None:
            todays_pnl += float(pnl)
            todays_count += 1
            if t.get("origin") == "auto":
                bot_pnl += float(pnl)
                bot_count += 1
            else:
                manual_pnl += float(pnl)

    return {
        "bot_active": bot_active,
        "open_trades": open_count,
        "todays_pnl_usd": round(todays_pnl, 2),
        "todays_closed_count": todays_count,
        "todays_bot_pnl_usd": round(bot_pnl, 2),
        "todays_bot_closed_count": bot_count,
        "todays_manual_pnl_usd": round(manual_pnl, 2),
        # iter-158 review P0-1/2/3 — broker position truth + effective state
        **(await _quick_truth_safe(db, user_id, open_count)),
    }


async def _quick_truth_safe(db, user_id: str, open_count: int) -> dict:
    """Fail-SAFE: on any error report UNKNOWN truth (never fabricated zero)
    and keep PANIC available."""
    try:
        from state_contract import quick_truth
        return await quick_truth(db, user_id, open_count)
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger("bot_routes").warning(
            "quick_truth failed for %s: %s", user_id, e)
        return {"position_truth": "UNKNOWN", "open_trades_broker": None,
                "effective_state": "BLOCKED", "panic_available": True,
                "accounts_total": None, "accounts_enabled": None,
                "bots_enabled": None, "eas_connected": None,
                "as_of": datetime.now(timezone.utc).isoformat()}


@router.get("/status")
async def get_bot_status(account_id: Optional[str] = None,
                         user=Depends(get_current_user)):
    """Return rich bot runtime status: last signal, last tick, why-no-trade, next tick ETA.

    Status is scoped to the default config when account_id omitted, or to the
    per-account override when supplied.
    """
    db = get_db()
    # When the caller doesn't specify an account, prefer the *active* per-account
    # config whose account is online (heartbeat fresh) — that's the bot the user
    # actually sees firing trades. Falls back to the default profile when no
    # per-account config qualifies.
    auto_selected_account = None
    if not account_id:
        per_acc_cfgs = await db.bot_configs.find({
            "user_id": user["id"],
            "active": True,
            "account_id": {"$ne": None},
        }).to_list(length=20)
        if per_acc_cfgs:
            now_utc = datetime.now(timezone.utc)
            for pac in per_acc_cfgs:
                pac_aid = pac.get("account_id")
                if not pac_aid:
                    continue
                try:
                    acc_oid = ObjectId(pac_aid)
                except Exception:
                    continue
                acc = await db.accounts.find_one({"_id": acc_oid})
                if not acc:
                    continue
                if acc.get("mode") == "paper":
                    auto_selected_account = pac["account_id"]
                    break
                hb = acc.get("last_heartbeat")
                try:
                    hb_dt = datetime.fromisoformat(str(hb).replace("Z", "+00:00"))
                    if (now_utc - hb_dt).total_seconds() <= 300:
                        auto_selected_account = pac["account_id"]
                        break
                except Exception:
                    continue
            if not auto_selected_account:
                auto_selected_account = per_acc_cfgs[0]["account_id"]
    effective_account = account_id or auto_selected_account
    cfg = await _get_or_create_config(db, user["id"], effective_account)
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

    # Anti-tilt freeze: bot_runner.py silently halts auto-execute when the last
    # N closed trades all lost within the freeze window. Mirror the exact logic
    # here so the UI can show the user *why* no trades are firing + a countdown.
    anti_tilt_frozen_until = None
    anti_tilt_reason = None
    if cfg.get("anti_tilt_enabled", True):
        atn = int(cfg.get("anti_tilt_consecutive_losses", 3) or 0)
        ath = int(cfg.get("anti_tilt_freeze_hours", 4) or 0)
        if atn > 0 and ath > 0:
            at_q = {"user_id": user["id"], "status": "closed"}
            if cfg.get("account_id"):
                at_q["account_id"] = cfg["account_id"]
            at_recent = await db.trades.find(at_q).sort("closed_at", -1).limit(atn).to_list(length=atn)
            if len(at_recent) == atn and all(float(r.get("pnl") or 0) < 0 for r in at_recent):  # iter-45: strict losses only
                last_close = at_recent[0].get("closed_at")
                try:
                    lc = datetime.fromisoformat(str(last_close).replace("Z", "+00:00"))
                    unfreeze_at = lc + timedelta(hours=ath)
                    remaining = (unfreeze_at - datetime.now(timezone.utc)).total_seconds()
                    if remaining > 0:
                        anti_tilt_frozen_until = unfreeze_at.isoformat()
                        mins_left = int(remaining // 60)
                        hrs, mins = divmod(mins_left, 60)
                        eta = f"{hrs}h {mins}m" if hrs else f"{mins}m"
                        anti_tilt_reason = (
                            f"Anti-tilt freeze: last {atn} trades lost — auto-execute "
                            f"paused for {eta}. Adjust in Bot Config → Capital Preservation."
                        )
                except Exception:
                    pass

    why_no_trade = None
    if not cfg.get("active"):
        why_no_trade = "Bot is stopped — start it from Bot Config"
    elif not cfg.get("auto_execute"):
        why_no_trade = "Auto-execute is OFF — signals generated but trades require manual click"
    elif anti_tilt_reason:
        why_no_trade = anti_tilt_reason
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
            retcode_hint = (" (INVALID_STOPS — SL/TP violated the broker's minimum stop distance. "
                            "EA v1.38 auto-clamps stops to broker rules and retries — update your EA from the Accounts page.)")
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
        "anti_tilt_frozen_until": anti_tilt_frozen_until,
        "cooldown_remaining_sec": cooldown_remaining_sec,
        "intelligence": intelligence,
        "scope_account_id": effective_account,
        "scope_auto_selected": bool(auto_selected_account and not account_id),
    }
