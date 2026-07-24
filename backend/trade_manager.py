"""Trade Manager — Profit Protection Suite (pip-based, 3-tier partial close)

Runs every TRADE_MANAGER_INTERVAL_SEC (default 15s) and for each open trade:
  1. At +100 pips profit  → close 50% AND move SL to entry (break-even)
  2. At +200 pips profit  → close another 25% (now 25% remains)
  3. At +300 pips profit  → close the last 25%
  4. Tracks daily realised drawdown and trips the circuit breaker if breached

Modifications are written to the trade doc as a `pending_modification` block.
The MT5 EA picks them up via /api/bridge/poll-trades and applies on its next tick,
then reports back via /api/bridge/modification-ack to clear the flag.
"""
import os
import asyncio
import logging
from datetime import datetime, timezone
from bson import ObjectId

from database import get_db
from market import get_quote
from ws_manager import manager as ws_manager
from pip_utils import price_to_pips
from notifier import notify_circuit_breaker

logger = logging.getLogger("trade-manager")

# Default pip targets (override per-trade if signal provided them)
# user policy (2026-07-16): TP1 100 / TP2 200, bank half at TP1, rest at TP2
DEFAULT_SL_PIPS = 120
DEFAULT_TP_PIPS = (100, 200, 200)   # (TP1, TP2, TP3) — tp3<=tp2 → 2-tier
DEFAULT_BE_TRIGGER_PIPS = 100        # move SL to entry at +100 pips


def _interval() -> int:
    return int(os.environ.get("TRADE_MANAGER_INTERVAL_SEC", "15"))


def _pips_in_profit(entry: float, current: float, action: str, symbol: str) -> float:
    """How many pips price has moved in favour of the trade."""
    diff = (current - entry) if action == "BUY" else (entry - current)
    return price_to_pips(symbol, diff)


def _trade_targets(trade: dict) -> tuple:
    """Resolve (sl_pips, tp1_pips, tp2_pips, tp3_pips) for a trade.

    Uses values stored on the trade doc; falls back to module defaults.
    """
    sl_pips = trade.get("sl_pips") or DEFAULT_SL_PIPS
    tp_pips = trade.get("tp_pips") or list(DEFAULT_TP_PIPS)
    tp1, tp2, tp3 = (tp_pips + [None, None, None])[:3]
    tp1 = tp1 or DEFAULT_TP_PIPS[0]
    tp2 = tp2 or DEFAULT_TP_PIPS[1]
    tp3 = tp3 or DEFAULT_TP_PIPS[2]
    return float(sl_pips), float(tp1), float(tp2), float(tp3)


def _soft_stop_should_fire(cfg: dict, pips_up: float, sl_pips: float, opened_at) -> bool:
    """iter-41 · Soft-Stop — should this losing trade be cut early?

    Fires when the adverse move reaches `soft_stop_loss_fraction` of the SL
    distance AND the trade is older than `soft_stop_min_minutes` (so normal
    entry noise doesn't insta-close fresh trades)."""
    if not cfg.get("soft_stop_enabled") or pips_up >= 0 or sl_pips <= 0:
        return False
    frac = min(max(float(cfg.get("soft_stop_loss_fraction") or 0.6), 0.3), 0.9)
    min_age_min = int(cfg.get("soft_stop_min_minutes") or 10)
    try:
        dt = datetime.fromisoformat(str(opened_at).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - dt).total_seconds() < min_age_min * 60:
            return False
    except Exception:
        pass  # unknown age — don't block the loss guard
    return (-pips_up) >= sl_pips * frac


async def _manage_one_trade(trade: dict, cfg: dict) -> None:
    """Decide if a single open trade needs a modification, write it to DB."""
    db = get_db()
    trade_id = trade["_id"]
    symbol = trade["symbol"]
    action = trade["action"]
    entry = float(trade.get("entry_price") or 0)
    original_lot = float(trade.get("original_lot_size") or trade.get("lot_size") or 0)
    current_lot = float(trade.get("lot_size") or 0)
    if entry <= 0 or original_lot <= 0:
        return
    # Phase B — scalp fast-path trades are managed EXCLUSIVELY by the scalp
    # engine's adaptive layer (per-second EV/trailing/partials); the tiered
    # TP manager must never contend for their pending_modification slot.
    if trade.get("scope") == "scalp_fast":
        return
    if trade.get("pending_modification"):
        return  # waiting for EA to apply previous modification

    # Skip paper trades — they're settled by execution.settle_paper_trades_against_price
    mode = (trade.get("mode") or "live").lower()
    if mode == "paper":
        return
    if not trade.get("mt5_ticket"):
        return  # still pending, EA hasn't filled yet

    try:
        quote = await get_quote(symbol)
        current = float(quote.get("price") or 0)
    except Exception as e:
        logger.warning("price fetch failed for %s: %s", symbol, e)
        return
    if current <= 0:
        return

    _, tp1_pips, tp2_pips, tp3_pips = _trade_targets(trade)
    pips_up = _pips_in_profit(entry, current, action, symbol)

    # Tier 3 — close remaining at +tp3 pips
    if (not trade.get("tp3_closed")
            and trade.get("tp2_closed")
            and pips_up >= tp3_pips):
        new_lot = 0.0  # Full close requested via close_requested flag
        await db.trades.update_one(
            {"_id": trade_id},
            {"$set": {
                "close_requested": True,
                "tp3_closed": True,
                "pending_modification": {
                    "type": "FULL_CLOSE",
                    "requested_at": datetime.now(timezone.utc).isoformat(),
                },
            }},
        )
        await ws_manager.broadcast(trade["user_id"], "trade_management", {
            "trade_id": str(trade_id),
            "action": "FULL_CLOSE_TP3",
            "from_lot": current_lot,
            "to_lot": new_lot,
            "pips": round(pips_up, 1),
        })
        # NOTE: no Telegram here — fires from modification_ack on EA confirm.
        return

    # Tier 2 — at +tp2 pips.
    # Two-tier mode (user policy 2026-07-16): when tp3 <= tp2 there is no
    # third target — half was banked at TP1, the REST closes fully here.
    # Legacy 3-tier mode: close another 25% (remaining = 25% of original);
    # let_winners_run: full size is still on at TP2 (no TP1 banking), so
    # close half here — remaining = 50% of original rides to TP3.
    if (not trade.get("tp2_closed")
            and trade.get("tp1_closed")
            and pips_up >= tp2_pips):
        if tp3_pips <= tp2_pips:
            await db.trades.update_one(
                {"_id": trade_id},
                {"$set": {
                    "close_requested": True,
                    "tp2_closed": True,
                    "tp3_closed": True,
                    "pending_modification": {
                        "type": "FULL_CLOSE",
                        "requested_at": datetime.now(timezone.utc).isoformat(),
                    },
                }},
            )
            await ws_manager.broadcast(trade["user_id"], "trade_management", {
                "trade_id": str(trade_id),
                "action": "FULL_CLOSE_TP2",
                "from_lot": current_lot,
                "to_lot": 0.0,
                "pips": round(pips_up, 1),
            })
            return
        tier2_frac = 0.5 if cfg.get("let_winners_run") else 0.25
        new_lot = round(original_lot * tier2_frac, 2)
        if new_lot >= 0.01:
            await db.trades.update_one(
                {"_id": trade_id},
                {"$set": {
                    "pending_modification": {
                        "type": "PARTIAL_CLOSE",
                        "new_volume": new_lot,
                        "requested_at": datetime.now(timezone.utc).isoformat(),
                    },
                    "tp2_target_lot": new_lot,
                }},
            )
            await ws_manager.broadcast(trade["user_id"], "trade_management", {
                "trade_id": str(trade_id),
                "action": "PARTIAL_CLOSE_TP2",
                "from_lot": current_lot,
                "to_lot": new_lot,
                "pips": round(pips_up, 1),
            })
            # NOTE: no Telegram here — fires from modification_ack on EA confirm.
            return

    # Tier 1 — close 50% AND move SL to entry at +tp1 pips.
    # let_winners_run (iter-41): move SL to break-even ONLY — keep full size
    # running toward TP2/TP3 to raise the average win.
    if not trade.get("tp1_closed") and pips_up >= tp1_pips:
        if cfg.get("let_winners_run"):
            await db.trades.update_one(
                {"_id": trade_id},
                {"$set": {
                    "tp1_closed": True,
                    "pending_modification": {
                        "type": "MODIFY_SL",
                        "new_sl": round(entry, 5),
                        "requested_at": datetime.now(timezone.utc).isoformat(),
                    },
                    "be_target_sl": round(entry, 5),
                }},
            )
            await ws_manager.broadcast(trade["user_id"], "trade_management", {
                "trade_id": str(trade_id),
                "action": "BE_ONLY_TP1_WINNERS_RUN",
                "from_lot": current_lot,
                "to_lot": current_lot,
                "new_sl": round(entry, 5),
                "pips": round(pips_up, 1),
            })
            return
        new_lot = round(original_lot * 0.5, 2)
        if new_lot >= 0.01:
            await db.trades.update_one(
                {"_id": trade_id},
                {"$set": {
                    "pending_modification": {
                        "type": "PARTIAL_CLOSE",
                        "new_volume": new_lot,
                        "new_sl": round(entry, 5),  # also move SL to entry on the EA modification
                        "requested_at": datetime.now(timezone.utc).isoformat(),
                    },
                    "tp1_target_lot": new_lot,
                    "be_target_sl": round(entry, 5),
                }},
            )
            await ws_manager.broadcast(trade["user_id"], "trade_management", {
                "trade_id": str(trade_id),
                "action": "PARTIAL_CLOSE_TP1_AND_BE",
                "from_lot": current_lot,
                "to_lot": new_lot,
                "new_sl": round(entry, 5),
                "pips": round(pips_up, 1),
            })
            # NOTE: no Telegram here — both partial-close and break-even
            # alerts fire from modification_ack once the EA confirms the move.
            return

    # Phase 2 · Adaptive exit engine — runs only when the tiered TP logic
    # above found nothing to do this tick (tier actions return early).
    # Strictly protective: TP ladder rescales to live ATR, SL only ever
    # tightens on momentum fade, size only ever shrinks into resistance.
    try:
        from adaptive_exits import manage_exits
        await manage_exits(db, trade, cfg, current, pips_up)
    except Exception as e:  # noqa: BLE001
        logger.warning("adaptive exits failed (fail-open) trade=%s: %s",
                       trade_id, e)


async def _check_daily_drawdown(cfg: dict) -> None:
    """If today's realised P&L falls below -daily_drawdown_pct of starting equity,
    auto-stop the bot for this cfg and trip the circuit breaker.

    Multi-account isolation: when `cfg.account_id` is set the check is scoped
    to that single account — losses on a different broker won't trip this
    cfg. Only the matching cfg is disabled (not all of the user's cfgs).
    """
    if not cfg.get("daily_drawdown_enabled", True):
        return
    if not cfg.get("active"):
        return
    db = get_db()
    user_id = cfg["user_id"]
    cfg_account_id = cfg.get("account_id")

    today_utc = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today_iso = today_utc.isoformat()

    # Sum today's realised P&L — account-scoped if cfg is per-account.
    trade_q: dict = {
        "user_id": user_id,
        "status": "closed",
        "closed_at": {"$gte": today_iso},
    }
    if cfg_account_id:
        trade_q["account_id"] = cfg_account_id
    closed = await db.trades.find(trade_q).to_list(length=500)
    today_pnl = sum(float(t.get("pnl") or 0) for t in closed)

    # Starting equity — account-scoped if cfg is per-account, else user-wide.
    acct_q: dict = {"user_id": user_id}
    if cfg_account_id:
        from bson import ObjectId
        try:
            acct_q["_id"] = ObjectId(cfg_account_id)
        except Exception:
            return
    accts = await db.accounts.find(acct_q).to_list(length=20)
    equity = sum(float(a.get("balance") or a.get("initial_balance") or 0) for a in accts) or 0
    if equity <= 0:
        return

    limit_pct = float(cfg.get("daily_drawdown_pct", 3.0))
    drawdown_pct = (today_pnl / equity) * 100.0
    if drawdown_pct <= -limit_pct:
        # Trip — disable only THIS cfg (not every active cfg user-wide).
        reason = f"Daily drawdown {drawdown_pct:.2f}% breached -{limit_pct:.2f}% limit"
        await db.bot_configs.update_one(
            {"_id": cfg["_id"]},
            {"$set": {
                "active": False,
                "circuit_breaker_tripped_at": datetime.now(timezone.utc).isoformat(),
                "circuit_breaker_reason": reason,
            }},
        )
        await ws_manager.broadcast(user_id, "circuit_breaker_tripped", {
            "reason": reason,
            "today_pnl": round(today_pnl, 2),
            "equity": round(equity, 2),
            "account_id": cfg_account_id,
        })
        try:
            await notify_circuit_breaker(
                user_id, reason, round(today_pnl, 2), round(equity, 2),
                account_id=cfg_account_id,
            )
        except Exception:
            pass
        logger.warning(
            "circuit breaker tripped user=%s acct=%s drawdown=%.2f%%",
            user_id, cfg_account_id or "default", drawdown_pct,
        )


async def _tick() -> None:
    """One pass of the trade manager loop."""
    db = get_db()
    # Collect all open trades
    open_trades = await db.trades.find({
        "status": "open",
        "mt5_ticket": {"$ne": None},
    }).to_list(length=200)

    # Daily drawdown check — iterate over EACH active bot_config so that
    # per-account configs get their checks scoped to that single account.
    active_cfgs = await db.bot_configs.find({"active": True}).to_list(length=200)
    for cfg in active_cfgs:
        try:
            await _check_daily_drawdown(cfg)
        except Exception as e:
            logger.exception(
                "daily drawdown check failed cfg=%s acct=%s: %s",
                cfg.get("_id"), cfg.get("account_id") or "default", e,
            )

    # Per-trade management — pick the cfg matching the trade's account_id
    # (or the default cfg) so trade management honours per-account settings.
    cfgs_by_user_acct: dict = {}
    for c in active_cfgs:
        cfgs_by_user_acct[(c["user_id"], c.get("account_id"))] = c
    # Also fetch inactive default cfgs for users who have one (still want
    # trade-management even if the bot is paused after a manual stop).
    extra_users = {t["user_id"] for t in open_trades} - {c["user_id"] for c in active_cfgs}
    for uid in extra_users:
        c = await db.bot_configs.find_one({"user_id": uid})
        if c:
            cfgs_by_user_acct[(uid, c.get("account_id"))] = c

    for t in open_trades:
        acct_id = t.get("account_id")
        cfg = (cfgs_by_user_acct.get((t["user_id"], acct_id))
               or cfgs_by_user_acct.get((t["user_id"], None)))
        if not cfg:
            continue
        try:
            await _manage_one_trade(t, cfg)
        except Exception as e:
            logger.exception("trade management failed trade_id=%s: %s", t.get("_id"), e)


async def run_loop() -> None:
    """Forever loop — started from server.py startup."""
    while True:
        try:
            await _tick()
        except Exception as e:
            logger.exception("trade_manager tick crashed: %s", e)
        await asyncio.sleep(_interval())
