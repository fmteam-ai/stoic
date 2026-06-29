from datetime import datetime, timezone
from typing import Optional
import logging
from fastapi import APIRouter, HTTPException
from bson import ObjectId
from pymongo.errors import DuplicateKeyError
from pydantic import BaseModel

logger = logging.getLogger(__name__)

from database import get_db
from models import BridgeHeartbeat, BridgeTradeReport, BridgeExternalDeal
from ws_manager import manager as ws_manager
from pip_utils import price_to_pips
from intelligence_counters import increment as inc_intel_counter
from trade_reconciler import reconcile_account, reconcile_user

router = APIRouter(prefix="/bridge", tags=["bridge"])


async def _account_by_token(token: str) -> dict:
    db = get_db()
    acc = await db.accounts.find_one({"bridge_token": token})
    if not acc:
        raise HTTPException(status_code=401, detail="Invalid bridge token")
    return acc


@router.post("/heartbeat")
async def heartbeat(payload: BridgeHeartbeat):
    db = get_db()
    acc = await _account_by_token(payload.bridge_token)
    now_iso = datetime.now(timezone.utc).isoformat()
    # Diagnostic: log the position snapshot size on every heartbeat so we can
    # tell whether the EA is sending the v1.25 `positions` field at all.
    pos_count = len(payload.positions) if payload.positions is not None else -1
    logger.info(
        "HB account=%s open_positions=%s positions_field=%s login=%s",
        acc.get("label") or str(acc["_id"])[-6:],
        payload.open_positions, pos_count, payload.account_login,
    )

    # First-pass mismatch check (v1.24+) — if the EA's MT5 login differs from
    # STOIC's configured account_number, we DO NOT trust the balance/equity it
    # reports. Persist the warning, null out the stale numbers so the UI shows
    # "—" instead of a misleading mirror of the wrong account's value.
    mismatch = False
    mismatch_reason: Optional[str] = None
    if payload.account_login is not None:
        configured = str(acc.get("account_number") or "").strip()
        reported = str(payload.account_login).strip()
        if configured and reported and configured != reported:
            mismatch = True
            mismatch_reason = (
                f"EA is logged into MT5 account {reported}, "
                f"but this STOIC account is configured for {configured}. "
                "Attach the EA to the correct MT5 terminal."
            )

    set_doc = {
        "balance": payload.balance if not mismatch else None,
        "equity": payload.equity if not mismatch else None,
        "open_positions": payload.open_positions if not mismatch else 0,
        "status": "connected" if not mismatch else "disconnected",
        "last_heartbeat": now_iso,
    }
    if payload.spreads:
        # Normalise keys + clamp to non-negative floats
        clean = {}
        for sym, sp in payload.spreads.items():
            try:
                clean[str(sym).upper()] = max(0.0, float(sp))
            except Exception:
                continue
        if clean:
            set_doc["current_spreads"] = clean
            set_doc["spreads_updated_at"] = now_iso

    # EA v1.24+: persist the broker-side login + currency so the Accounts UI
    # can display the actual account the EA is reading from. We already
    # computed `mismatch` at the top of this function (used to null out
    # balance) — here we just record the values and clear/keep the flag.
    if payload.account_login is not None:
        set_doc["broker_account_id_reported"] = payload.account_login
        set_doc["broker_account_mismatch"] = mismatch
        set_doc["broker_account_mismatch_reason"] = mismatch_reason
    if payload.base_currency:
        set_doc["broker_currency_reported"] = payload.base_currency.upper()

    # EA v1.26+: track the EA's reported semantic version so the Dashboard
    # can flag terminals running stale builds (missing history-sweep, etc.).
    if payload.client_version:
        set_doc["ea_version"] = str(payload.client_version)
        set_doc["ea_version_updated_at"] = now_iso

    # iter-76 · EA v1.34+: auto-detect the broker's symbol-suffix convention
    # from the MarketWatch inventory. Stored on the account so `execution.py`
    # can route orders without the user having to set `symbol_suffix` by hand.
    # User-set `symbol_suffix` (manual override) always takes precedence.
    if payload.available_symbols:
        from broker_symbol_detector import infer_broker_suffix
        detection = infer_broker_suffix(payload.available_symbols)
        set_doc["auto_detected_symbol_suffix"] = detection["suffix"]
        set_doc["auto_detected_suffix_confidence"] = detection["confidence"]
        set_doc["auto_detected_suffix_bases"] = detection["matched_bases"]
        set_doc["auto_detected_suffix_at"] = now_iso
        # iter-81: also persist the raw inventory so the admin UI and
        # diagnostic flows can SEE what the broker actually offers (e.g.
        # `XAUUSD.b`, `GOLDcfd`, `XAU/USD`). Without this, when detection
        # gives a wrong answer we have no way to recover short of asking
        # the user to dig into MetaTrader manually. Cap at 200 symbols.
        set_doc["available_symbols"] = list(payload.available_symbols)[:200]
        set_doc["available_symbols_at"] = now_iso

    # EA v1.22+: persist the ticket list so the user can later trigger
    # manual reconciliation even if a heartbeat isn't currently in flight.
    #
    # EA v1.25+ stopped sending `open_tickets` as a top-level field but does
    # send the full `positions` array — derive the ticket list from there so
    # the reconciler keeps working on every heartbeat. Without this, the DB's
    # `open_tickets` would be permanently STUCK at whatever the last legacy
    # heartbeat sent (the original 696559793... bug).
    if payload.open_tickets is None and payload.positions:
        payload.open_tickets = [int(p.ticket) for p in payload.positions
                                if getattr(p, "ticket", None) is not None]

    reconcile_summary = None
    if payload.open_tickets is not None:
        try:
            tickets = [int(t) for t in payload.open_tickets if t is not None]
        except (TypeError, ValueError):
            tickets = []

        # STALE-TICKET DETECTION: some EA builds (pre-v1.26) cache the tickets
        # array and never purge entries after MT5 closes them — heartbeats then
        # arrive with `open_positions=0` but `open_tickets=[4 dead ones]`.
        # `open_positions` comes from PositionsTotal() and is authoritative.
        # When the two disagree and positions reports FEWER, the tickets list
        # is stale → use the count as ground truth and let reconcile close
        # the leftovers in DB. (When positions reports MORE we keep the
        # tickets list — safer to under-close than to over-close.)
        ea_positions_count = payload.open_positions
        if (ea_positions_count is not None
                and ea_positions_count < len(tickets)):
            logger.warning(
                "EA stale-tickets detected: positions=%s but %s tickets reported "
                "for account=%s. Treating tickets as []; user should upgrade EA "
                "to v1.26 for OnTradeTransaction + history sweep.",
                ea_positions_count, len(tickets), str(acc["_id"]),
            )
            tickets = []  # force orphan sweep; revive sweep also skipped below

        set_doc["open_tickets"] = tickets
        set_doc["open_tickets_updated_at"] = now_iso

        # TICKET-LEVEL AUTO-REVIVE: if STOIC has closed a trade but the
        # broker is still reporting its ticket as open, STOIC was wrong.
        # Revive it before reconcile_account runs — otherwise the EA would
        # never get it back under bot control. This catches the failure
        # mode of slippage_veto / reconciler / Force Sync wiping a trade
        # whose ticket is still live on the broker. Works for v1.22+ EAs
        # that only send open_tickets (not full positions snapshot).
        #
        # Skip when tickets is empty after stale detection — otherwise we
        # would loop-revive trades the broker has actually closed.
        if tickets:
            revive_candidates = await db.trades.find({
                "account_id": str(acc["_id"]),
                "mt5_ticket": {"$in": tickets},
                "status": "closed",
                "exit_price": None,
            }).to_list(length=50)
            for t in revive_candidates:
                await db.trades.update_one(
                    {"_id": t["_id"]},
                    {"$set": {
                        "status": "open", "closed_at": None,
                        "revived_at": now_iso,
                        "revived_from_close_reason": t.get("close_reason"),
                        "revived_via_open_tickets": True,
                        "pending_modification": None,
                    },
                     "$unset": {
                         "close_reason": "", "reconciled": "",
                         "close_requested": "",
                     }},
                )
                logger.info(
                    "Auto-revived trade %s (ticket %s) — broker still has it open "
                    "(prior close_reason=%s)",
                    t["_id"], t.get("mt5_ticket"), t.get("close_reason"),
                )

        # Auto-reconcile on every heartbeat — closes orphans within ~5s of EA tick.
        reconcile_summary = await reconcile_account(
            str(acc["_id"]), tickets, source="heartbeat",
        )

    await db.accounts.update_one({"_id": acc["_id"]}, {"$set": set_doc})

    # EA v1.25+: ingest the live positions snapshot. Auto-creates trade
    # records for any open position STOIC doesn't yet track. Skipped on
    # mismatch — we don't want to suck wrong-account positions into the
    # STOIC profile they were misrouted to.
    backfilled = 0
    revived = 0
    live_ticks: list[dict] = []  # v1.27 broker-real-time price relay
    if payload.positions is not None and not mismatch:
        account_id = str(acc["_id"])
        for p in payload.positions:
            # EA v1.27+: relay the broker-live tick to the UI regardless of
            # whether the trade is already tracked or being backfilled. We
            # accumulate then broadcast once at the end so a 4-position
            # heartbeat sends ONE WS frame, not four.
            if p.current_price is not None:
                live_ticks.append({
                    "ticket": int(p.ticket),
                    "symbol": p.symbol,
                    "current_price": float(p.current_price),
                    "profit": float(p.profit or 0.0),
                })
            # Already tracked?
            existing = await db.trades.find_one({
                "account_id": account_id, "mt5_ticket": int(p.ticket),
            })
            if existing:
                # AUTO-REVIVE: STOIC has the ticket but marked it closed
                # without an exit price — yet the broker still has the
                # position open. Premature close (panic + reconcile race).
                # Bring it back under bot control.
                if (existing.get("status") == "closed"
                        and existing.get("exit_price") is None):
                    await db.trades.update_one(
                        {"_id": existing["_id"]},
                        {"$set": {
                            "status": "open",
                            "closed_at": None,
                            "revived_at": now_iso,
                            "revived_from_close_reason": existing.get("close_reason"),
                            "revived_via_snapshot": True,
                            # Clear any leftover EA modification (FULL_CLOSE from
                            # slippage veto / reconciler) so the EA doesn't re-kill
                            # the revived position on its next poll.
                            "pending_modification": None,
                        },
                         "$unset": {
                             "close_reason": "", "reconciled": "",
                             "close_requested": "",
                         }},
                    )
                    revived += 1
                continue
            opened_iso = (
                datetime.fromtimestamp(p.time_open, tz=timezone.utc).isoformat()
                if p.time_open else now_iso
            )
            await db.trades.insert_one({
                "user_id": acc["user_id"],
                "account_id": account_id,
                "symbol": p.symbol,
                "action": p.type,
                "lot_size": p.volume,
                "entry_price": p.price_open,
                "stop_loss": p.sl or 0.0,
                "take_profit": p.tp or 0.0,
                "exit_price": None,
                "pnl": 0.0,
                "status": "open",
                "mode": "live",
                "broker": acc.get("broker", "MT5"),
                "mt5_ticket": int(p.ticket),
                "opened_at": opened_iso,
                "closed_at": None,
                "origin": ("manual" if p.magic == 0 else
                           "auto" if p.magic == 901234 else "other_ea"),
                "magic_number": int(p.magic or 0),
                "external_open": p.magic != 901234,
                "backfilled_from_snapshot": True,
            })
            backfilled += 1
        if backfilled > 0:
            await ws_manager.broadcast(acc["user_id"], "trades_backfilled", {
                "account_id": account_id, "count": backfilled,
            })
        if revived > 0:
            await ws_manager.broadcast(acc["user_id"], "trades_revived", {
                "account_id": account_id, "count": revived,
            })
        # v1.27 — broker-live ticks for the Trades page. Single frame per HB.
        if live_ticks:
            await ws_manager.broadcast(acc["user_id"], "position_ticks", {
                "account_id": account_id,
                "ticks": live_ticks,
                "ts": now_iso,
            })

    # The DB write (above) already nulls balance/equity on broker_account
    # mismatch so the user can't be fooled by a wrong-terminal heartbeat. The
    # WebSocket broadcast MUST honour the same filter — otherwise the live
    # UI update would happily overwrite "—" with the wrong account's balance
    # the moment a stray heartbeat arrived. This caused two STOIC rows
    # (Roboforex + VT Markets) to mirror the same $-value when both EAs
    # were attached to the same MT5 terminal.
    await ws_manager.broadcast(acc["user_id"], "account_heartbeat", {
        "account_id": str(acc["_id"]),
        "balance": payload.balance if not mismatch else None,
        "equity": payload.equity if not mismatch else None,
        "open_positions": payload.open_positions,
        "last_heartbeat": now_iso,
        "spreads": set_doc.get("current_spreads"),
        "broker_account_mismatch": mismatch,
        "broker_account_mismatch_reason": mismatch_reason,
        "broker_account_id_reported": payload.account_login,
        "status": "connected" if not mismatch else "disconnected",
    })
    resp = {"ok": True, "server_time": now_iso}
    if reconcile_summary and reconcile_summary["closed_count"] > 0:
        resp["reconciled"] = reconcile_summary
    return resp


class PollRequest(BaseModel):
    bridge_token: str


@router.post("/poll-trades")
async def poll_trades(payload: PollRequest):
    db = get_db()
    acc = await _account_by_token(payload.bridge_token)

    # 1. Pending NEW trades (status='pending')
    new_cursor = db.trades.find({"account_id": str(acc["_id"]), "status": "pending"})
    pending = await new_cursor.to_list(length=20)
    out = []
    for t in pending:
        out.append({
            "trade_id": str(t["_id"]),
            "symbol": t["symbol"],
            "action": t["action"],
            "lot_size": t["lot_size"],
            "entry_price": t["entry_price"],
            "stop_loss": t["stop_loss"],
            "take_profit": t["take_profit"],
            "close_requested": t.get("close_requested", False),
            "mt5_ticket": t.get("mt5_ticket"),
        })

    # 2. Open trades with pending modifications (break-even / partial-close / trailing)
    mod_cursor = db.trades.find({
        "account_id": str(acc["_id"]),
        "status": "open",
        "mt5_ticket": {"$ne": None},
        "pending_modification": {"$exists": True, "$ne": None},
    })
    mods = await mod_cursor.to_list(length=20)
    modifications = []
    for t in mods:
        m = t.get("pending_modification") or {}
        modifications.append({
            "trade_id": str(t["_id"]),
            "mt5_ticket": t.get("mt5_ticket"),
            "symbol": t["symbol"],
            "type": m.get("type"),
            "new_sl": m.get("new_sl"),
            "new_tp": m.get("new_tp"),
            "new_volume": m.get("new_volume"),
        })

    return {"trades": out, "modifications": modifications}


class BridgeModificationAck(BaseModel):
    bridge_token: str
    trade_id: str
    type: str   # MODIFY_SL | PARTIAL_CLOSE
    success: bool = True
    new_sl: float | None = None
    new_volume: float | None = None
    error: str | None = None


@router.post("/modification-ack")
async def modification_ack(payload: BridgeModificationAck):
    """EA acknowledges it applied a pending_modification on its end."""
    db = get_db()
    acc = await _account_by_token(payload.bridge_token)
    trade = await db.trades.find_one({"_id": ObjectId(payload.trade_id)})
    if not trade or trade["account_id"] != str(acc["_id"]):
        raise HTTPException(status_code=404, detail="Trade not found")

    # Notifications to fire AFTER the EA confirms — collected here, dispatched
    # only on the success path so the user never gets a Telegram for an action
    # the broker never actually applied.
    pending_notifs: list = []

    update = {"pending_modification": None}
    if payload.success:
        if payload.type == "MODIFY_SL" and payload.new_sl is not None:
            update["stop_loss"] = float(payload.new_sl)
            if not trade.get("breakeven_set"):
                # Mark BE only when SL moved to/past entry
                entry = float(trade.get("entry_price") or 0)
                action = trade.get("action")
                hit_be = (action == "BUY" and payload.new_sl >= entry) or \
                         (action == "SELL" and payload.new_sl <= entry)
                if hit_be:
                    update["breakeven_set"] = True
                    pending_notifs.append(("breakeven", float(payload.new_sl), 1.0))
            else:
                update["trail_active"] = True
                pending_notifs.append(("trail", float(payload.new_sl), None))
        elif payload.type == "PARTIAL_CLOSE" and payload.new_volume is not None:
            from_lot = float(trade.get("lot_size") or 0)
            to_lot = float(payload.new_volume)
            update["lot_size"] = to_lot
            update["partial_closed"] = True
            update["partial_closed_at"] = datetime.now(timezone.utc).isoformat()
            # If this PC also carried a new_sl (Tier-1 combo move), apply it
            mod = trade.get("pending_modification") or {}
            mod_new_sl = mod.get("new_sl")
            if mod_new_sl is not None:
                update["stop_loss"] = float(mod_new_sl)
                update["breakeven_set"] = True
            # Mark tier progression + compute R for the alert
            if not trade.get("tp1_closed"):
                update["tp1_closed"] = True
                r_mult = 1.0
            elif not trade.get("tp2_closed"):
                update["tp2_closed"] = True
                r_mult = 2.0
            else:
                r_mult = 3.0
            pending_notifs.append(("partial_close", from_lot, to_lot, r_mult))
            if mod_new_sl is not None:
                pending_notifs.append(("breakeven", float(mod_new_sl), 1.0))
        elif payload.type == "FULL_CLOSE":
            update["tp3_closed"] = True
    else:
        update["last_modification_error"] = payload.error or "unknown EA error"

    await db.trades.update_one({"_id": ObjectId(payload.trade_id)}, {"$set": update})
    await ws_manager.broadcast(acc["user_id"], "trade_updated", {
        "trade_id": payload.trade_id,
        **update,
    })

    # Dispatch the queued Telegram alerts now that the broker has confirmed.
    if pending_notifs:
        try:
            from notifier import (
                notify_breakeven, notify_partial_close, notify_trail,
            )
            for n in pending_notifs:
                kind = n[0]
                if kind == "partial_close":
                    _, from_lot, to_lot, r_mult = n
                    await notify_partial_close(
                        acc["user_id"], payload.trade_id, from_lot, to_lot, r_mult,
                    )
                elif kind == "breakeven":
                    _, new_sl, r_mult = n
                    await notify_breakeven(
                        acc["user_id"], payload.trade_id, new_sl, r_mult,
                    )
                elif kind == "trail":
                    _, new_sl, _r = n
                    # r_multiple unknown at server side for pure trailing — pass 1+
                    await notify_trail(
                        acc["user_id"], payload.trade_id, new_sl, 1.0,
                    )
        except Exception as e:
            logger.warning("modification-ack notify dispatch failed: %s", e)

    return {"ok": True}


@router.post("/report")
async def report_trade(payload: BridgeTradeReport):
    db = get_db()
    acc = await _account_by_token(payload.bridge_token)
    trade = await db.trades.find_one({"_id": ObjectId(payload.trade_id)})
    if not trade or trade["account_id"] != str(acc["_id"]):
        raise HTTPException(status_code=404, detail="Trade not found")

    update = {"status": payload.status}
    if payload.mt5_ticket is not None:
        update["mt5_ticket"] = payload.mt5_ticket

    # Slippage veto — on first OPEN report, compare actual fill vs intended entry
    slippage_force_close = False
    if (
        payload.status == "open"
        and payload.entry_price is not None
        and not trade.get("slippage_checked")
    ):
        intended = float(trade.get("entry_price") or 0)
        actual = float(payload.entry_price)
        symbol = trade.get("symbol") or ""
        slip_pips = price_to_pips(symbol, abs(actual - intended)) if intended > 0 else 0.0
        update["intended_entry_price"] = intended
        update["slippage_pips"] = round(slip_pips, 2)
        update["slippage_checked"] = True
        # Pull bot config for the threshold — prefer the per-account override
        # if it exists, else fall back to the user's default profile.
        account_id_str = str(acc["_id"])
        cfg = (
            await db.bot_configs.find_one(
                {"user_id": acc["user_id"], "account_id": account_id_str}
            )
            or await db.bot_configs.find_one({
                "user_id": acc["user_id"],
                "$or": [{"account_id": None}, {"account_id": {"$exists": False}}],
            })
            or {}
        )
        if cfg.get("slippage_veto_enabled", True):
            caps = cfg.get("max_slippage_pips") or {"XAUUSD": 20.0, "BTCUSD": 80.0}
            cap = float(caps.get(symbol, caps.get(symbol.upper(), 9999)))
            if slip_pips > cap:
                slippage_force_close = True
                # Stamp requested_at so the stuck-modification health check
                # can age this out after 10min if the EA ignores it.
                update["pending_modification"] = {
                    "type": "FULL_CLOSE",
                    "requested_at": datetime.now(timezone.utc).isoformat(),
                    "reason": "slippage_veto",
                }
                update["close_reason"] = "slippage_veto"
                update["slippage_veto_cap_pips"] = cap

    if payload.entry_price is not None:
        update["entry_price"] = payload.entry_price
    if payload.exit_price is not None:
        update["exit_price"] = payload.exit_price
    if payload.pnl is not None:
        update["pnl"] = payload.pnl
    if payload.error:
        update["error"] = payload.error
    if payload.status == "closed":
        update["closed_at"] = datetime.now(timezone.utc).isoformat()
        # Infer close_reason if not already set (manual/panic/telegram set it pre-emptively).
        if not trade.get("close_reason"):
            entry = float(trade.get("entry_price") or 0)
            sl = float(trade.get("stop_loss") or 0)
            tp3 = float(trade.get("tp3") or trade.get("take_profit") or 0)
            exit_p = float(payload.exit_price or 0)
            action = trade.get("action")
            close_reason = "broker"
            if exit_p > 0 and entry > 0:
                # Within 0.1% of SL → SL hit. Within 0.1% of TP → TP hit.
                tol = max(entry * 0.001, 0.5)
                if sl > 0 and abs(exit_p - sl) <= tol:
                    close_reason = "stop_loss"
                elif tp3 > 0 and abs(exit_p - tp3) <= tol:
                    close_reason = "take_profit"
                else:
                    # Profit direction inference
                    profit_dir = (action == "BUY" and exit_p > entry) or (action == "SELL" and exit_p < entry)
                    close_reason = "take_profit" if profit_dir else "stop_loss"
            update["close_reason"] = close_reason

    await db.trades.update_one({"_id": ObjectId(payload.trade_id)}, {"$set": update})
    if slippage_force_close:
        try:
            await inc_intel_counter(acc["user_id"], "slippage_veto")
        except Exception:
            pass
    await ws_manager.broadcast(acc["user_id"], "trade_updated", {
        "trade_id": payload.trade_id,
        **update,
    })

    # Telegram alerts — fire-and-forget
    try:
        full_trade = await db.trades.find_one({"_id": ObjectId(payload.trade_id)})
        if full_trade:
            from notifier import notify_trade_opened, notify_trade_closed
            if payload.status == "open" and not trade.get("notified_opened"):
                await notify_trade_opened(acc["user_id"], full_trade)
                await db.trades.update_one(
                    {"_id": ObjectId(payload.trade_id)}, {"$set": {"notified_opened": True}}
                )
            elif payload.status == "closed":
                await notify_trade_closed(acc["user_id"], full_trade)
    except Exception:
        pass

    # Loss post-mortem + auto-loosen on winners — fire-and-forget. Skips itself
    # if not eligible.
    if payload.status == "closed":
        try:
            from loss_postmortem import maybe_record_postmortem, maybe_record_winner
            from drift_detector import record_residual_for_trade
            import asyncio
            asyncio.create_task(maybe_record_postmortem(db, payload.trade_id))
            asyncio.create_task(maybe_record_winner(db, payload.trade_id))
            asyncio.create_task(record_residual_for_trade(db, payload.trade_id))
        except Exception:
            pass

    return {"ok": True}


@router.post("/external-deal")
async def external_deal(payload: BridgeExternalDeal):
    """Report any broker-side deal — bot-initiated AND manual.

    Fired from the EA's OnTradeTransaction handler. Catches the trades that
    the old /bridge/report path missed (manual close on MT5, partial-close
    from the broker UI, manual position open, etc.) so STOIC's view always
    matches the broker.

    Idempotency: `deal_id` is the broker's unique identifier per deal. We
    upsert into `broker_deals` with a unique index so the same deal can
    never be applied twice (covers EA retries on flaky network).
    """
    db = get_db()
    acc = await _account_by_token(payload.bridge_token)
    account_id = str(acc["_id"])
    user_id = acc["user_id"]

    # 1. Idempotency check + audit log
    deal_doc = {
        "deal_id": payload.deal_id,
        "account_id": account_id,
        "user_id": user_id,
        "mt5_ticket": payload.mt5_ticket,
        "deal_entry": payload.deal_entry,
        "symbol": payload.symbol,
        "action": payload.action,
        "lots": payload.lots,
        "price": payload.price,
        "profit": payload.profit,
        "commission": payload.commission,
        "swap": payload.swap,
        "deal_time": payload.deal_time,
        "magic": payload.magic,
        "received_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        await db.broker_deals.insert_one(deal_doc)
    except DuplicateKeyError:
        # We already processed this (deal_id, account_id). Safe no-op.
        return {"ok": True, "duplicate": True, "deal_id": payload.deal_id}
    except Exception as e:
        # Real failure — surface so the EA can retry. Silently swallowing here
        # would lose broker fills.
        logger.exception("broker_deals.insert_one failed deal_id=%s acct=%s: %s",
                         payload.deal_id, account_id, e)
        raise HTTPException(status_code=500, detail="Failed to persist broker deal")

    # Best-effort ISO timestamp from broker (unix seconds → UTC ISO).
    if payload.deal_time:
        deal_iso = datetime.fromtimestamp(payload.deal_time, tz=timezone.utc).isoformat()
    else:
        deal_iso = datetime.now(timezone.utc).isoformat()

    # 2. Look up matching STOIC trade by (account, mt5_ticket).
    existing = await db.trades.find_one({
        "account_id": account_id,
        "mt5_ticket": payload.mt5_ticket,
    })

    realized = float(payload.profit) + float(payload.commission) + float(payload.swap)
    # STOIC's MT5 EA stamps every order with magic=901234. Anything else means
    # the trade did NOT originate from the bot:
    #   • magic == 0           → manually opened in the MT5 terminal
    #   • magic == STOIC_MAGIC → STOIC bot
    #   • any other non-zero   → a DIFFERENT EA running on the same account
    STOIC_MAGIC = 901234
    if payload.magic == 0:
        trade_origin = "manual"
    elif payload.magic == STOIC_MAGIC:
        trade_origin = "auto"
    else:
        trade_origin = "other_ea"
    is_external = (trade_origin != "auto")

    if payload.deal_entry == "in":
        # OPEN event — only insert if STOIC doesn't already track this ticket.
        if existing:
            return {"ok": True, "noop": "ticket_already_tracked"}
        trade_doc = {
            "user_id": user_id,
            "account_id": account_id,
            "symbol": payload.symbol,
            "action": payload.action,
            "lot_size": payload.lots,
            "entry_price": payload.price,
            "stop_loss": 0.0,
            "take_profit": 0.0,
            "exit_price": None,
            "pnl": 0.0,
            "status": "open",
            "mode": "live",
            "broker": acc.get("broker", "MT5"),
            "mt5_ticket": payload.mt5_ticket,
            "opened_at": deal_iso,
            "closed_at": None,
            "origin": trade_origin,
            "magic_number": int(payload.magic or 0),
            "external_open": is_external,
        }
        result = await db.trades.insert_one(trade_doc)
        tid = str(result.inserted_id)
        await ws_manager.broadcast(user_id, "trade_updated", {
            "trade_id": tid, "status": "open", "external_open": is_external,
            "symbol": payload.symbol, "action": payload.action,
        })
        if is_external:
            try:
                from notifier import notify_trade_opened
                trade_doc["_id"] = result.inserted_id
                await notify_trade_opened(user_id, trade_doc)
            except Exception:
                pass
        return {"ok": True, "created": tid, "external_open": is_external}

    # deal_entry == "out" or "inout" → CLOSE event
    # PARTIAL vs FULL CLOSE DISCRIMINATION:
    # A position's "out" deal can be either a full close (deal.lots ==
    # position.lot_size) or a partial close (deal.lots < position.lot_size,
    # the remainder stays open). Treating every "out" as full was the
    # 696627605 / 696637253 bug — STOIC closed positions that the broker
    # still had open with reduced size.
    is_partial_close = False
    if existing and existing.get("status") == "open":
        prior_lot = float(existing.get("lot_size") or 0)
        deal_lot = float(payload.lots or 0)
        # 1% tolerance for floating-point rounding by the broker
        if prior_lot > 0 and deal_lot > 0 and deal_lot < prior_lot * 0.99:
            is_partial_close = True

    if is_partial_close:
        new_lot = round(float(existing["lot_size"]) - float(payload.lots), 2)
        update = {
            "lot_size": max(new_lot, 0.01),
            "partial_closed": True,
            "partial_closed_at": deal_iso,
            "broker_deal_id": payload.deal_id,
            "pending_modification": None,   # broker confirmed the partial
        }
        if not existing.get("original_lot_size"):
            update["original_lot_size"] = float(existing["lot_size"])
        # Accumulate realised P&L on the closed portion
        prior_pnl = float(existing.get("pnl") or 0)
        update["pnl"] = round(prior_pnl + float(realized or 0), 2)
        await db.trades.update_one(
            {"_id": existing["_id"]}, {"$set": update},
        )
        # Tag the already-persisted deal as a partial close so the audit
        # trail shows it correctly. The outer flow above already saved
        # the deal_doc with deal_entry="out" before we got here.
        await db.broker_deals.update_one(
            {"deal_id": payload.deal_id, "account_id": str(acc["_id"])},
            {"$set": {"deal_entry_kind": "partial_out"}},
        )
        await ws_manager.broadcast(user_id, "trade_updated", {
            "trade_id": str(existing["_id"]),
            "lot_size": update["lot_size"],
            "partial_closed": True,
            "pnl": update["pnl"],
        })
        logger.info(
            "Partial-close ingested for ticket=%s: %s → %s lots (deal=%s lots)",
            payload.mt5_ticket, existing.get("lot_size"), update["lot_size"], payload.lots,
        )
        return {"ok": True, "updated": str(existing["_id"]),
                "partial_close": True, "new_lot_size": update["lot_size"]}

    # Full close (or "out" deal for a trade STOIC didn't know was open) — original path.
    update = {
        "exit_price": payload.price,
        "pnl": realized,
        "status": "closed",
        "closed_at": deal_iso,
        "broker_deal_id": payload.deal_id,
    }
    if existing:
        if existing.get("exit_price") is not None:
            # /bridge/report already filled this; just confirm.
            update = {
                "broker_deal_id": payload.deal_id,
                "external_confirmation": True,
            }
        else:
            existing_reason = existing.get("close_reason")
            update["close_reason"] = (
                f"{existing_reason}+external_close" if existing_reason
                else ("external_close" if is_external else "broker_confirmed")
            )
            update["backfilled"] = True
            update["backfilled_at"] = datetime.now(timezone.utc).isoformat()
            # Distinguish "auto-repaired from reconciler ghost" vs "first-time
            # close report" — useful in the Audit Trail when STOIC initially
            # had no exit_price and the EA later caught up.
            if existing.get("status") == "closed":
                update["auto_repaired_from_ghost"] = True
                update["auto_repaired_at"] = datetime.now(timezone.utc).isoformat()
        await db.trades.update_one({"_id": existing["_id"]}, {"$set": update})
        tid = str(existing["_id"])
    else:
        # Close event with no matching trade — user opened AND closed on MT5
        # without STOIC ever tracking it. Insert a fully-closed audit row so
        # the P&L still lands in their account stats.
        trade_doc = {
            "user_id": user_id,
            "account_id": account_id,
            "symbol": payload.symbol,
            "action": payload.action,
            "lot_size": payload.lots,
            "entry_price": payload.price,
            "stop_loss": 0.0,
            "take_profit": 0.0,
            "mode": "live",
            "broker": acc.get("broker", "MT5"),
            "mt5_ticket": payload.mt5_ticket,
            "origin": "external",
            "external_open": is_external,
            "external_close": True,
            "opened_at": deal_iso,
            "broker_deal_id": payload.deal_id,
            **update,
        }
        result = await db.trades.insert_one(trade_doc)
        tid = str(result.inserted_id)

    await ws_manager.broadcast(user_id, "trade_updated", {
        "trade_id": tid, **update,
    })

    if existing and existing.get("exit_price") is None:
        try:
            from notifier import notify_trade_closed
            full = await db.trades.find_one({"_id": ObjectId(tid)})
            if full:
                await notify_trade_closed(user_id, full)
        except Exception:
            pass

    return {"ok": True, "updated": tid, "pnl": realized}
