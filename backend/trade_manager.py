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
from close_commands import request_close
from command_fence import stamp_pending_modification
from market import get_quote
from ws_manager import manager as ws_manager
from pip_utils import price_to_pips
from strategy_engines import SCALP_ENGINES
from notifier import notify_circuit_breaker

logger = logging.getLogger("trade-manager")

# Fix plan A2/B7 — a tier modification may only be written while the position
# is still open, has NO close in flight and holds no other pending modification;
# it must never overwrite an in-flight FULL_CLOSE (PANIC / manual / TP3).
def _free_slot_filter(trade_id) -> dict:
    return {"_id": trade_id, "status": "open", "close_requested": {"$ne": True},
            "$or": [{"pending_modification": None}, {"pending_modification": {"$exists": False}}]}

# Default pip targets (override per-trade if signal provided them)
# user policy (2026-07-16): TP1 100 / TP2 200, bank half at TP1, rest at TP2
DEFAULT_SL_PIPS = 120
DEFAULT_TP_PIPS = (100, 200, 200)   # (TP1, TP2, TP3) — tp3<=tp2 → 2-tier

# v1.60.32 C8/C9 — hf_scalp / hf_scalp_fast exit rules (owner request, demo engines). Before the single-TP change
# the breakeven rode on TP1 = 0.4 × 2R = 0.8R; a trade reached +0.72R and then took the full stop. Now: stop to
# entry (+ round-trip costs) at +0.5R, and a trade that has not reached +0.5R within 90 minutes is closed — the
# momentum burst has failed (all three losses so far were held 1–3 h after a failed burst).
SCALP_BE_TRIGGER_R = float(os.environ.get("SCALP_BE_TRIGGER_R", "0.5"))
SCALP_TIME_STOP_MIN = int(os.environ.get("SCALP_TIME_STOP_MIN", "90"))
SCALP_DEFAULT_COST_PIPS = {"XAUUSD": 2.5, "XAGUSD": 3.0, "USDJPY": 1.2}   # pip_utils convention (gold pip = 0.1)
SCALP_DEFAULT_COST_PIPS_FX = 1.5


def tightened_sl(trade: dict, new_sl: float):
    """Never loosen a stop (14 Oct 15:13 incident: the legacy TP1 path moved two SELL stops from entry−costs back up
    to raw entry). Returns `new_sl` only when it is strictly tighter than the current stop — BUY: higher, SELL: lower —
    or when the trade has no stop yet; otherwise None."""
    cur = float(trade.get("stop_loss") or 0)
    if cur <= 0:
        return new_sl
    if trade.get("action") == "BUY":
        return new_sl if new_sl > cur else None
    return new_sl if new_sl < cur else None


def be_clamp(trade: dict, new_sl: float, current: float, min_stop_distance_px) -> dict | None:
    """v1.60.42 — the EA clamps a MODIFY_SL to the broker's stops level measured from the CURRENT price; on a fast
    spike back toward entry the clamped breakeven lands on the LOSING side of entry (OnEquity #46719148: SL clamped
    below entry, −0.92). Pure: returns the clamp detail when the requested stop would be clamped past entry, else None."""
    try:
        dist = float(min_stop_distance_px or 0)
    except (TypeError, ValueError):
        return None
    if dist <= 0 or not current or current <= 0:
        return None
    entry = float(trade.get("entry_price") or 0)
    if trade.get("action") == "BUY":
        clamped = min(float(new_sl), current - dist)
        losing = clamped < entry
    else:
        clamped = max(float(new_sl), current + dist)
        losing = clamped > entry
    if not losing:
        return None
    return {"requested_sl": round(float(new_sl), 5), "clamped_to": round(clamped, 5), "entry": entry, "current": current,
            "min_stop_distance_px": dist, "action": trade.get("action")}


async def be_clamped_skip(db, trade: dict, new_sl: float, current: float, source: str) -> dict | None:
    """Skip (and log `be_clamped`) a breakeven MODIFY_SL that the EA would clamp below entry. Needs fresh broker
    symbol specs on the account (protection_guard.broker_stop_constraints); without them the request goes out as before."""
    from protection_guard import broker_stop_constraints, find_account
    acc, _ = await find_account(db, str(trade.get("account_id") or ""))
    cons = broker_stop_constraints(acc, trade.get("symbol")) if acc else None
    clamp = be_clamp(trade, new_sl, current, (cons or {}).get("min_stop_distance_px"))
    if clamp is None:
        return None
    clamp.update({"source": source, "at": datetime.now(timezone.utc).isoformat(), "decision": "skip"})
    logger.warning("be_clamped: %s %s #%s stop %.5f would be clamped to %.5f (entry %.5f, stops level %.5f) — not sent (%s)",
                   trade.get("symbol"), trade.get("action"), trade.get("mt5_ticket"), clamp["requested_sl"], clamp["clamped_to"],
                   clamp["entry"], clamp["min_stop_distance_px"], source)
    last = (trade.get("be_clamped") or [None])[-1]
    if last and last.get("source") == source and last.get("requested_sl") == clamp["requested_sl"]:
        try:
            if (datetime.now(timezone.utc) - datetime.fromisoformat(str(last.get("at")))).total_seconds() < 300:
                return clamp                        # same clamp re-evaluated within 5 min: evidence already on the trade
        except (TypeError, ValueError):
            pass
    await db.trades.update_one({"_id": trade["_id"]}, {"$push": {"be_clamped": clamp}, "$inc": {"be_clamped_count": 1},
                                                       "$set": {"be_clamped_last_at": clamp["at"]}})
    await ws_manager.broadcast(trade["user_id"], "trade_management", {
        "trade_id": str(trade["_id"]), "action": "BE_CLAMPED", "requested_sl": clamp["requested_sl"],
        "clamped_to": clamp["clamped_to"], "source": source})
    return clamp


async def record_sl_loosen_refusal(trade: dict, requested_sl: float, source: str) -> bool:
    """Stop audit trail (v1.60.35): persist each REFUSED stop-loosen on the trade — once per trade + requested SL +
    source (the manager re-evaluates every cycle; the first refusal is the evidence, repeats are noise). Returns True
    when a new entry was written."""
    req = round(float(requested_sl), 5)
    for r in trade.get("sl_loosen_refusals") or []:
        if r.get("source") == source and round(float(r.get("requested_sl") or 0), 5) == req:
            return False
    entry = {"source": source, "requested_sl": req, "current_sl": float(trade.get("stop_loss") or 0),
             "action": trade.get("action"), "at": datetime.now(timezone.utc).isoformat()}
    await get_db().trades.update_one({"_id": trade["_id"]}, {"$push": {"sl_loosen_refusals": entry}})
    lst = trade.setdefault("sl_loosen_refusals", [])
    if entry not in lst:
        lst.append(entry)
    logger.warning("stop-loosen REFUSED trade=%s %s %s: requested %s vs current %s", trade["_id"], trade.get("symbol"),
                   source, req, entry["current_sl"])
    return True


def is_scalp_engine_trade(trade: dict) -> bool:
    """Bot trades carry the engine in `scope` ("hf_scalp" / "hf_scalp_fast"); `engine` is usually None on real rows
    (review of v1.60.32 on live tickets) — honour either."""
    return (trade.get("engine") or trade.get("scope")) in SCALP_ENGINES


def scalp_cost_pips(trade: dict) -> float:
    """Round-trip cost in pips: the spread recorded at entry when known, a conservative per-symbol default otherwise."""
    from pip_utils import base_symbol
    spread = trade.get("spread_pips") or trade.get("entry_spread_pips") or trade.get("spread_at_entry_pips")
    try:
        spread = float(spread or 0)
    except (TypeError, ValueError):
        spread = 0.0
    if spread <= 0:
        spread = SCALP_DEFAULT_COST_PIPS.get(base_symbol(trade.get("symbol")), SCALP_DEFAULT_COST_PIPS_FX)
    return spread + float(trade.get("commission_pips") or 0)


def scalp_breakeven_sl(trade: dict) -> float:
    """Entry plus costs, on the profitable side — the trade can never close negative after this stop is live."""
    from pip_utils import pips_to_price
    entry = float(trade["entry_price"])
    off = pips_to_price(trade["symbol"], scalp_cost_pips(trade))
    return round(entry + off if trade.get("action") == "BUY" else entry - off, 5)


def scalp_time_stop_due(trade: dict, now=None) -> bool:
    """True when the trade is older than SCALP_TIME_STOP_MIN and never armed the +0.5R breakeven.
    Intended (audit item 9, decided 16 Oct): once the breakeven is armed the trade is risk-free (stop at entry +
    round-trip costs) and is LEFT TO RUN to its TP — no second time-stop. A scratch close is the broker stop doing
    its job, not a timeout."""
    if trade.get("breakeven_set") or trade.get("be_target_sl") or trade.get("tp1_closed"):
        return False
    opened = trade.get("opened_at") or trade.get("created_at")
    try:
        dt = datetime.fromisoformat(str(opened).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return False
    return ((now or datetime.now(timezone.utc)) - dt).total_seconds() >= SCALP_TIME_STOP_MIN * 60


async def _scalp_engine_exits(db, trade: dict, cfg: dict, current: float, pips_up: float) -> bool:
    """C8 breakeven at +0.5R (stop → entry + costs) and C9 90-min time-stop. Returns True when it acted."""
    sl_pips, *_ = _trade_targets(trade)
    trade_id = trade["_id"]
    if sl_pips > 0 and pips_up >= SCALP_BE_TRIGGER_R * sl_pips \
            and not trade.get("breakeven_set") and not trade.get("be_target_sl"):
        new_sl = scalp_breakeven_sl(trade)
        if tightened_sl(trade, new_sl) is None:
            await record_sl_loosen_refusal(trade, new_sl, "scalp_breakeven_0_5r")
        elif await be_clamped_skip(db, trade, new_sl, current, "scalp_breakeven_0_5r"):
            return True                             # v1.60.42 — retried next cycle once price clears the stops level
        else:
            stamped = await stamp_pending_modification(
                db, _free_slot_filter(trade_id),
                {"type": "MODIFY_SL", "new_sl": new_sl, "reason": "scalp_breakeven_0_5r"},
                {"be_target_sl": new_sl, "scalp_exit_rule": "breakeven_0_5r",
                 "scalp_be_armed_at": datetime.now(timezone.utc).isoformat(),
                 "scalp_be_armed_pips": round(pips_up, 1)})
            if stamped is None:
                return True
            await ws_manager.broadcast(trade["user_id"], "trade_management", {
                "trade_id": str(trade_id), "action": "SCALP_BE_0_5R", "new_sl": new_sl,
                "from_lot": float(trade.get("lot_size") or 0), "to_lot": float(trade.get("lot_size") or 0),
                "pips": round(pips_up, 1)})
            return True
    if scalp_time_stop_due(trade):
        await request_close(db, {"_id": trade_id}, reason="time_stop", actor="trade_manager", stamp={
            "time_stop_minutes": SCALP_TIME_STOP_MIN, "scalp_exit_rule": "time_stop_90m",
            "scalp_time_stop_pips": round(pips_up, 1),
            "pending_modification": {"type": "FULL_CLOSE", "reason": "scalp_time_stop",
                                     "requested_at": datetime.now(timezone.utc).isoformat()}})
        await ws_manager.broadcast(trade["user_id"], "trade_management", {
            "trade_id": str(trade_id), "action": "SCALP_TIME_STOP", "pips": round(pips_up, 1),
            "from_lot": float(trade.get("lot_size") or 0), "to_lot": 0.0})
        return True
    return False
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
        quote = await get_quote(symbol, allow_stale=False)   # v1.60.50 — never manage a live tier on a stale price
        current = float(quote.get("price") or 0)
    except Exception as e:
        logger.warning("price fetch failed for %s: %s", symbol, e)
        return
    if current <= 0:
        return

    _, tp1_pips, tp2_pips, tp3_pips = _trade_targets(trade)
    pips_up = _pips_in_profit(entry, current, action, symbol)

    # v1.60.32 C8/C9 — hf_scalp / hf_scalp_fast exits (demo engines): breakeven at +0.5R, 90-minute time-stop.
    if is_scalp_engine_trade(trade):
        if await _scalp_engine_exits(db, trade, cfg, current, pips_up):
            return

    # Tier 3 — close remaining at +tp3 pips
    if (not trade.get("tp3_closed")
            and trade.get("tp2_closed")
            and pips_up >= tp3_pips):
        new_lot = 0.0  # Full close requested via close_requested flag
        await request_close(db, {"_id": trade_id}, reason="take_profit", actor="trade_manager", stamp={
            "tp3_closed": True,
            "pending_modification": {
                "type": "FULL_CLOSE",
                "requested_at": datetime.now(timezone.utc).isoformat(),
            },
        })
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
            await request_close(db, {"_id": trade_id}, reason="take_profit", actor="trade_manager", stamp={
                "tp2_closed": True,
                "tp3_closed": True,
                "pending_modification": {
                    "type": "FULL_CLOSE",
                    "requested_at": datetime.now(timezone.utc).isoformat(),
                },
            })
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
            stamped = await stamp_pending_modification(
                db, _free_slot_filter(trade_id),
                {"type": "PARTIAL_CLOSE", "new_volume": new_lot},
                {"tp2_target_lot": new_lot})
            if stamped is None:
                return  # close in flight / slot taken — never overwrite it
            await ws_manager.broadcast(trade["user_id"], "trade_management", {
                "trade_id": str(trade_id),
                "action": "PARTIAL_CLOSE_TP2",
                "from_lot": current_lot,
                "to_lot": new_lot,
                "pips": round(pips_up, 1),
            })
            # NOTE: no Telegram here — fires from modification_ack on EA confirm.
            return

    # v1.60.42 — a TP1 breakeven deferred by be_clamped (partial banked, stop left alone): retry once the price has
    # cleared the broker's stops level, so the trade still gets its breakeven instead of riding the full stop.
    if (trade.get("tp1_closed") and trade.get("be_clamped") and not trade.get("be_target_sl")
            and not trade.get("breakeven_set")):
        be_sl = tightened_sl(trade, round(entry, 5))
        if be_sl is not None and not await be_clamped_skip(db, trade, be_sl, current, "tp1_be_retry"):
            stamped = await stamp_pending_modification(
                db, _free_slot_filter(trade_id),
                {"type": "MODIFY_SL", "new_sl": be_sl, "reason": "tp1_be_retry"},
                {"be_target_sl": be_sl})
            if stamped is not None:
                await ws_manager.broadcast(trade["user_id"], "trade_management", {
                    "trade_id": str(trade_id), "action": "BE_RETRY_AFTER_CLAMP", "new_sl": be_sl,
                    "from_lot": current_lot, "to_lot": current_lot, "pips": round(pips_up, 1)})
                return

    # Tier 1 — close 50% AND move SL to entry at +tp1 pips.
    # let_winners_run (iter-41): move SL to break-even ONLY — keep full size
    # running toward TP2/TP3 to raise the average win.
    if not trade.get("tp1_closed") and pips_up >= tp1_pips:
        be_sl = tightened_sl(trade, round(entry, 5))   # None → the stop is already at/beyond entry: leave it alone
        if be_sl is None:
            await record_sl_loosen_refusal(trade, round(entry, 5),
                                           "tp1_let_winners_run" if cfg.get("let_winners_run") else "tp1_partial_close")
        elif await be_clamped_skip(db, trade, be_sl, current,
                                   "tp1_let_winners_run" if cfg.get("let_winners_run") else "tp1_partial_close"):
            if cfg.get("let_winners_run"):
                return                              # v1.60.42 — BE-only step: nothing sent, retried next cycle
            be_sl = None                            # the partial still banks; the BE is retried above once clear
        if cfg.get("let_winners_run"):
            if be_sl is None:
                await db.trades.update_one(_free_slot_filter(trade_id), {"$set": {"tp1_closed": True}})
                return
            stamped = await stamp_pending_modification(
                db, _free_slot_filter(trade_id),
                {"type": "MODIFY_SL", "new_sl": be_sl},
                {"tp1_closed": True, "be_target_sl": be_sl})
            if stamped is None:
                return
            await ws_manager.broadcast(trade["user_id"], "trade_management", {
                "trade_id": str(trade_id),
                "action": "BE_ONLY_TP1_WINNERS_RUN",
                "from_lot": current_lot,
                "to_lot": current_lot,
                "new_sl": be_sl,
                "pips": round(pips_up, 1),
            })
            return
        new_lot = round(original_lot * 0.5, 2)
        if new_lot >= 0.01:
            pend = {"type": "PARTIAL_CLOSE", "new_volume": new_lot}
            extra = {"tp1_target_lot": new_lot}
            if be_sl is not None:
                pend["new_sl"] = be_sl           # also move SL to entry on the EA modification — only if it tightens
                extra["be_target_sl"] = be_sl
            stamped = await stamp_pending_modification(db, _free_slot_filter(trade_id), pend, extra)
            if stamped is None:
                return
            await ws_manager.broadcast(trade["user_id"], "trade_management", {
                "trade_id": str(trade_id),
                "action": "PARTIAL_CLOSE_TP1_AND_BE",
                "from_lot": current_lot,
                "to_lot": new_lot,
                "new_sl": be_sl,
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


# v1.60.48 (audit item 13) — _check_daily_drawdown REMOVED: circuit_breakers.py is the single daily-drawdown breaker
# (auto trades, equity-based, policy-capped). This one judged ALL trades against balance with no policy cap.


async def _tick() -> None:
    """One pass of the trade manager loop."""
    db = get_db()
    # Collect all open trades
    open_trades = await db.trades.find({
        "status": "open",
        "mt5_ticket": {"$ne": None},
    }).to_list(length=200)

    active_cfgs = await db.bot_configs.find({"active": True}).to_list(length=200)

    # Per-trade management — pick the cfg matching the trade's account_id
    # (or the default cfg) so trade management honours per-account settings.
    cfgs_by_user_acct: dict = {}
    for c in active_cfgs:
        cfgs_by_user_acct[(c["user_id"], c.get("account_id"))] = c
    # v1.60.48 (audit item 16) — a trade whose per-account config is PAUSED (active=False) used to fall back to the
    # user's default config (different tiers / breakers). Load the trade's own account config whenever it exists;
    # the default config is only the fallback for accounts without one.
    for t in open_trades:
        key = (t["user_id"], t.get("account_id"))
        if key not in cfgs_by_user_acct and t.get("account_id"):
            c = await db.bot_configs.find_one({"user_id": t["user_id"], "account_id": t["account_id"]})
            cfgs_by_user_acct[key] = c          # None is cached too → falls back to the default below
    # Also fetch default cfgs (active or paused) for every user with open trades: the default manages accounts
    # without their own config, and keeps managing after a manual stop.
    for uid in {t["user_id"] for t in open_trades}:
        if (uid, None) in cfgs_by_user_acct:
            continue
        c = await db.bot_configs.find_one(
            {"user_id": uid, "$or": [{"account_id": None}, {"account_id": {"$exists": False}}]}) \
            or await db.bot_configs.find_one({"user_id": uid})
        if c:
            cfgs_by_user_acct.setdefault((uid, c.get("account_id")), c)

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
        started = datetime.now(timezone.utc)
        try:
            await _tick()
        except Exception as e:
            logger.exception("trade_manager tick crashed: %s", e)
        try:
            from workers.base import persist_progress, record_progress
            record_progress("trade_manager.run_loop", processed=1,
                            started_at=started, interval_sec=_interval())
            await persist_progress(get_db(), "trade_manager.run_loop")
        except Exception as e:  # noqa: BLE001
            logger.warning("loop progress telemetry failed: %s", e)
        await asyncio.sleep(_interval())
