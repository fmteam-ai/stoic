from datetime import datetime, timezone, timedelta
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
from pip_utils import price_to_pips, base_symbol
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
    # EA v1.48+ — persist precise broker stop constraints per BASE symbol
    # (round 13 item 5); protection_guard consumes these when computing
    # emergency stops.
    if payload.symbol_specs:
        specs = {}
        for sym, spec in payload.symbol_specs.items():
            if spec.point > 0:
                specs[base_symbol(str(sym).upper())] = spec.model_dump()
        if specs:
            set_doc["symbol_specs"] = specs
            set_doc["symbol_specs_updated_at"] = now_iso

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
        #
        # iter-92: MERGE (union) with existing symbols instead of replacing.
        # Legacy EAs (v1.36 and earlier) don't include broker-aliases like
        # `GOLD#`/`SILVER#` in the MarketWatch scan (they filter against a
        # hardcoded XAU/XAG-only bases array). Admins can now inject the
        # missing tickers into the account doc and the next heartbeat will
        # PRESERVE them instead of overwriting them. On EA v1.37+ the EA
        # itself reports the aliases so no admin intervention is needed.
        existing = list(acc.get("available_symbols") or [])
        incoming = list(payload.available_symbols)[:200]
        merged: list[str] = []
        seen: set[str] = set()
        for s in incoming + existing:
            if isinstance(s, str) and s.strip() and s not in seen:
                merged.append(s)
                seen.add(s)
                if len(merged) >= 200:
                    break
        set_doc["available_symbols"] = merged
        set_doc["available_symbols_at"] = now_iso

    # EA v1.22+: persist the ticket list so the user can later trigger
    # manual reconciliation even if a heartbeat isn't currently in flight.
    #
    # EA v1.25+ stopped sending `open_tickets` as a top-level field but does
    # send the full `positions` array — derive the ticket list from there so
    # the reconciler keeps working on every heartbeat. Without this, the DB's
    # `open_tickets` would be permanently STUCK at whatever the last legacy
    # heartbeat sent (the original 696559793... bug).
    #
    # `is not None` (not truthiness) matters: an EXPLICITLY-EMPTY positions
    # array means "broker has zero open positions" — we must let that fall
    # through to reconcile so orphans in the DB get closed. The original
    # `and payload.positions:` check silently bypassed reconciliation when
    # the broker had nothing open, leaving the 2 orphans we just hit
    # (iter-90: VTMarkets + Tauro showing open in UI, gone on broker).
    if payload.open_tickets is None and payload.positions is not None:
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

    # iter-46 · Auto-heal: if closed trades carry estimated/unknown P&L, queue
    # a deep history sync so the EA re-pushes exact broker figures. Throttled
    # to one check per 5 min per account so heartbeats stay cheap.
    if not mismatch and not acc.get("pending_history_sync"):
        last_check = acc.get("ghost_check_at")
        due = True
        if last_check:
            try:
                due = (datetime.now(timezone.utc)
                       - datetime.fromisoformat(last_check)).total_seconds() > 300
            except ValueError:
                due = True
        if due:
            set_doc["ghost_check_at"] = now_iso
            seven_days_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            ghost_count = await db.trades.count_documents({
                "account_id": str(acc["_id"]),
                "status": "closed",
                "closed_at": {"$gte": seven_days_ago},
                "$or": [{"pnl_estimated": True}, {"pnl_unknown": True}],
            })
            if ghost_count > 0:
                set_doc["pending_history_sync"] = {
                    "lookback_seconds": 7 * 86400,
                    "requested_at": now_iso,
                    "requested_by": "auto_heal",
                    "ghost_count": ghost_count,
                }
                logger.info(
                    "Auto-heal queued deep broker sync for account=%s (%s ghost trades)",
                    str(acc["_id"]), ghost_count,
                )

    await db.accounts.update_one({"_id": acc["_id"]}, {"$set": set_doc})

    # Round 14 P0 — propagate the fresh snapshot into in-memory scalp
    # runners so readiness/staleness checks and sizing never depend on the
    # (possibly old) account doc preloaded by the last tick batch.
    try:
        from scalp.engine import update_account_snapshot
        update_account_snapshot(str(acc["_id"]), set_doc)
    except Exception:  # noqa: BLE001 — snapshot relay must never break HB
        logger.exception("scalp account-snapshot propagation failed")

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
                # iter-45 · Persist the broker-live P&L/price on the open
                # trade doc every heartbeat. If the close report is ever
                # missed (EA offline at TP/SL hit), the reconciler uses this
                # last-known snapshot as the ESTIMATED exit instead of
                # leaving the trade with no exit price and no P&L.
                if existing.get("status") == "open" and p.current_price is not None:
                    await db.trades.update_one(
                        {"_id": existing["_id"]},
                        {"$set": {
                            "live_pnl": float(p.profit or 0.0),
                            "live_price": float(p.current_price),
                            "live_at": now_iso,
                        }},
                    )
                # P0-1 · protection verification: the broker position
                # snapshot carries the ACTUAL live SL. A filled trade is
                # only OPEN once that SL is confirmed; a missing SL first
                # gets one re-arm (MODIFY_SL), then a forced close.
                lc = existing.get("lifecycle_state")
                if (existing.get("status") == "open"
                        and lc in ("FILLED_UNPROTECTED",
                                   "PROTECTION_REQUESTED",
                                   # audit P0 · legacy/recovery verification
                                   # path: a trade stuck at BROKER_ACCEPTED
                                   # is certified by the SAME broker-SL
                                   # check instead of a lifecycle shortcut
                                   "BROKER_ACCEPTED")):
                    from scalp import order_state as _os
                    tid_s = str(existing["_id"])
                    broker_sl = float(p.sl or 0)
                    requested_sl = float(existing.get("stop_loss") or 0)
                    prot = existing.get("protection") or {}
                    if broker_sl > 0:
                        await _os.apply(db, tid_s, _os.PROTECTED,
                                        f"prot:{tid_s}",
                                        meta={"sl": broker_sl})
                        await _os.apply(db, tid_s, _os.OPEN, f"open:{tid_s}")
                        await db.trades.update_one(
                            {"_id": existing["_id"]},
                            {"$set": {"confirmed_stop_loss": broker_sl,
                                      "protection.state": "PROTECTED",
                                      "protection.confirmed_at": now_iso,
                                      "protection.confirmed_sl": broker_sl}})
                    elif requested_sl > 0 and lc == "BROKER_ACCEPTED":
                        # legacy recovery: normalize into the protection
                        # chain; the next heartbeat re-arms or confirms
                        await _os.apply(db, tid_s, _os.FILLED_UNPROTECTED,
                                        f"filled:{tid_s}")
                    elif requested_sl > 0 and lc == "FILLED_UNPROTECTED":
                        st = await _os.apply(db, tid_s,
                                             _os.PROTECTION_REQUESTED,
                                             f"protreq:{tid_s}")
                        if st == "applied":
                            from command_fence import stamp_pending_modification
                            await stamp_pending_modification(
                                db,
                                {"_id": existing["_id"],
                                 "pending_modification": None},
                                {"type": "MODIFY_SL", "new_sl": requested_sl,
                                 "reason": "protection_rearm"},
                                extra_set={
                                    "protection.state": "REARM_REQUESTED",
                                    "protection.requested_at": now_iso})
                    elif requested_sl > 0 and lc == "PROTECTION_REQUESTED":
                        req_at = prot.get("requested_at") or prot.get("filled_at")
                        try:
                            age_s = (datetime.now(timezone.utc)
                                     - datetime.fromisoformat(str(req_at))
                                     ).total_seconds() if req_at else 0
                        except ValueError:
                            age_s = 0
                        if age_s > 90:
                            from command_fence import stamp_pending_modification
                            stamped = await stamp_pending_modification(
                                db,
                                {"_id": existing["_id"],
                                 "pending_modification": None},
                                {"type": "FULL_CLOSE",
                                 "reason": "unprotected_position"},
                                extra_set={
                                    "close_reason": "unprotected_position",
                                    "protection.state": "CLOSE_ESCALATED"})
                            if stamped is not None:
                                await _os.apply(
                                    db, tid_s, _os.CLOSE_REQUESTED,
                                    f"closereq:{tid_s}",
                                    meta={"reason": "unprotected_position"})
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
            # NOTE (iter-97): p.time_open comes from MT5 in broker-local
            # epoch seconds — labelling it UTC causes timestamp drift.
            # Use server-received UTC as the canonical opened_at instead.
            opened_iso = now_iso
            await db.trades.insert_one({
                "user_id": acc["user_id"],
                "account_id": account_id,
                "symbol": p.symbol,
                "action": p.type,
                "lot_size": p.volume,
                "entry_price": p.price_open,
                "stop_loss": p.sl or 0.0,
                # broker snapshot IS confirmed protection evidence (round 12)
                "confirmed_stop_loss": p.sl or 0.0,
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


class BridgeCandles(BaseModel):
    bridge_token: str
    symbol: str
    timeframe: str = "M15"
    bars: list


@router.post("/candles")
async def receive_candles(payload: BridgeCandles):
    """EA v1.42 — M15 candle feed for the Market Structure agent.

    P0-4 · every stage instrumented into `candle_feed_health` (per
    user/symbol/timeframe): receipt, symbol normalization, bar validation,
    merge and Mongo write — so a stale feed is diagnosable stage by stage.
    """
    db = get_db()
    account = await _account_by_token(payload.bridge_token)
    base = base_symbol(payload.symbol)
    now_iso = datetime.now(timezone.utc).isoformat()
    health_key = {"user_id": account["user_id"], "symbol": base,
                  "timeframe": payload.timeframe}
    bars = []
    dropped = 0
    for b in (payload.bars or [])[-200:]:
        try:
            bars.append({"t": int(b["t"]), "o": float(b["o"]), "h": float(b["h"]),
                         "l": float(b["l"]), "c": float(b["c"]),
                         "v": float(b.get("v") or 0)})
        except (KeyError, TypeError, ValueError):
            dropped += 1
            continue
    if not bars:
        await db.candle_feed_health.update_one(health_key, {"$set": {
            "last_received_at": now_iso, "source_symbol": payload.symbol,
            "account_id": str(account["_id"]),
            "bars_in_payload": len(payload.bars or []),
            "valid_bars": 0, "dropped_bars": dropped,
            "last_error": "all bars invalid", "last_write_ok": False,
        }, "$inc": {"payloads_received": 1, "payloads_rejected": 1}},
            upsert=True)
        raise HTTPException(status_code=422, detail="No valid bars")
    # iter-125 · Accumulate history server-side (EA only sends ~96 bars):
    # merge by timestamp, keep the newest 800 (8+ days of M15 → real 4H data).
    existing = await db.intraday_candles.find_one(
        {"user_id": account["user_id"], "symbol": base,
         "timeframe": payload.timeframe}, {"bars": 1})
    if existing and existing.get("bars"):
        merged = {int(b["t"]): b for b in existing["bars"]}
        merged.update({int(b["t"]): b for b in bars})
        bars = [merged[t] for t in sorted(merged)][-800:]
    write_ok = True
    try:
        await db.intraday_candles.update_one(
            {"user_id": account["user_id"], "symbol": base,
             "timeframe": payload.timeframe},
            {"$set": {"bars": bars, "source_symbol": payload.symbol,
                      "account_id": str(account["_id"]),
                      "updated_at": now_iso}},
            upsert=True)
    except Exception:
        write_ok = False
        raise
    finally:
        last_bar_ts = (max(int(b["t"]) for b in bars) if bars else None)
        bar_lag_s = (max(0, int(datetime.now(timezone.utc).timestamp())
                         - last_bar_ts) if last_bar_ts else None)
        await db.candle_feed_health.update_one(health_key, {"$set": {
            "last_received_at": now_iso, "source_symbol": payload.symbol,
            "account_id": str(account["_id"]),
            "bars_in_payload": len(payload.bars or []),
            "valid_bars": len(bars), "dropped_bars": dropped,
            "last_bar_ts": last_bar_ts, "bar_lag_s": bar_lag_s,
            "stored_total": len(bars), "last_write_ok": write_ok,
            "last_error": None,
        }, "$inc": {"payloads_received": 1}}, upsert=True)
    return {"status": "ok", "stored": len(bars)}


class BridgeTicks(BaseModel):
    bridge_token: str
    symbol: str
    sent_at_ms: int | None = None
    ticks: list = []


@router.post("/ticks")
async def receive_ticks(payload: BridgeTicks):
    """EA v1.44 — bid/ask tick stream for the scalp fast path."""
    db = get_db()
    account = await _account_by_token(payload.bridge_token)
    base = base_symbol(payload.symbol)
    from scalp.engine import get_runner, ensure_account_lease
    # Round 7 item 1 — enforced account ownership: a worker without the
    # distributed lease must not process ticks for this account (locally
    # cached, ~1 DB round-trip per half-TTL — off the per-tick hot path).
    if not await ensure_account_lease(db, str(account["_id"])):
        return {"status": "rejected", "reason": "account_owned_by_other_worker"}
    runner = get_runner(str(account["_id"]), account["user_id"], base)
    if runner is None:
        return {"status": "ignored", "reason": f"{base} not in approved scalp universe"}
    if not getattr(runner, "_hydrated", False):
        runner._hydrated = True
        cfg_doc = await db.scalp_configs.find_one(
            {"account_id": str(account["_id"]), "symbol": base})
        if cfg_doc and cfg_doc.get("removed"):
            from scalp.engine import _runners
            _runners.pop(f"{account['_id']}:{base}", None)
            return {"status": "ignored", "reason": "runner_removed"}
        if cfg_doc:
            runner.enabled = bool(cfg_doc.get("enabled"))
            runner.mode = cfg_doc.get("mode", "shadow")
            runner.commission_usd_per_lot_side = float(
                cfg_doc.get("commission_usd_per_lot_side") or 0.0)
        runner.broker = str(account.get("broker") or "")
        runner.account_type = str(account.get("account_type") or "")
        # item 9 — restore risk counters + reconcile open positions BEFORE
        # any new entry is permitted
        await runner.restore_risk(db)
        from scalp.model import load_persisted
        await load_persisted(db, runner.model_key())
    out = await runner.ingest(db, account, payload.ticks or [], payload.sent_at_ms)
    return {"status": "ok", **out}


class BridgeDom(BaseModel):
    bridge_token: str
    symbol: str
    bids: list = []
    asks: list = []


@router.post("/dom")
async def receive_dom(payload: BridgeDom):
    """EA v1.43 — Depth of Market snapshot for the Liquidity Mapping agent."""
    db = get_db()
    account = await _account_by_token(payload.bridge_token)
    base = base_symbol(payload.symbol)

    def _rows(rows):
        out = []
        for r in (rows or [])[:25]:
            try:
                out.append({"p": float(r["p"]), "v": float(r["v"])})
            except (KeyError, TypeError, ValueError):
                continue
        return out

    bids, asks = _rows(payload.bids), _rows(payload.asks)
    if not bids and not asks:
        return {"status": "ok", "stored": 0}
    await db.dom_snapshots.update_one(
        {"user_id": account["user_id"], "symbol": base},
        {"$set": {"bids": bids, "asks": asks,
                  "source_symbol": payload.symbol,
                  "account_id": str(account["_id"]),
                  "updated_at": datetime.now(timezone.utc).isoformat()}},
        upsert=True)
    return {"status": "ok", "stored": len(bids) + len(asks)}


@router.post("/poll-trades")
async def poll_trades(payload: PollRequest):
    db = get_db()
    acc = await _account_by_token(payload.bridge_token)

    # 1. Pending NEW trades (status='pending')
    # iter-96: atomic dispatch lock. Previously we returned EVERY pending
    # trade on every poll — so if /bridge/report was slow (or the response
    # was dropped), the EA would re-execute the same pending trade on its
    # next poll, opening a DUPLICATE broker position with a new ticket. The
    # original ticket eventually attached to the pending doc via a late
    # /bridge/report, but the duplicate came in via /bridge/external-deal
    # fresh-insert with SL=0/TP=0 (no signal_id, no adoption match).
    #
    # Fix: findAndModify each candidate — stamp `_dispatched_at` and only
    # return trades whose `_dispatched_at` is NULL or older than the
    # DISPATCH_LOCK window. If the EA never confirms the dispatch (report
    # never lands), the lock auto-expires and the trade gets re-dispatched.
    DISPATCH_LOCK_SEC = 30
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=DISPATCH_LOCK_SEC)).isoformat()
    out = []
    # Loop up to 20 times, each iteration atomically claims one pending doc.
    for _ in range(20):
        t = await db.trades.find_one_and_update(
            {
                "account_id": str(acc["_id"]),
                "status": "pending",
                "$or": [
                    {"_dispatched_at": {"$exists": False}},
                    {"_dispatched_at": None},
                    {"_dispatched_at": {"$lt": cutoff}},
                ],
            },
            {"$set": {"_dispatched_at": datetime.now(timezone.utc).isoformat(),
                      "submission_state": "sent_to_terminal"},
             "$inc": {"_dispatch_count": 1}},
        )
        if not t:
            break
        # Round 10/11 item 1 — fencing-epoch enforcement at the dispatch
        # fence. The AUTHORITATIVE current ownership document is the truth:
        # the order's epoch must EQUAL the current lease_epoch and the lease
        # must be unexpired. max_order_epoch is kept as an extra monotonic
        # safeguard only — it must not substitute for the live lease check
        # (a delayed order from a paused ex-owner could otherwise pass
        # before the new owner's first order raises the watermark).
        if t.get("scope") == "scalp_fast" and t.get("scalp_lease_epoch") is not None:
            ep = int(t.get("scalp_lease_epoch") or 0)
            now_iso = datetime.now(timezone.utc).isoformat()
            owner = await db.scalp_owners.find_one(
                {"account_id": str(acc["_id"])},
                {"lease_epoch": 1, "lease_until": 1, "max_order_epoch": 1})
            cur_epoch = int((owner or {}).get("lease_epoch") or 0)
            lease_live = bool(owner) and str(owner.get("lease_until") or "") > now_iso
            stale = ((not lease_live) or ep != cur_epoch
                     or ep < int((owner or {}).get("max_order_epoch") or 0))
            if stale:
                await db.trades.update_one(
                    {"_id": t["_id"]},
                    {"$set": {"status": "cancelled",
                              "error": "stale_scalp_lease_epoch",
                              "close_reason": "stale_scalp_lease_epoch",
                              "closed_at": now_iso}})
                logger.warning(
                    "rejected stale-epoch scalp order trade=%s epoch=%d "
                    "(owner epoch=%d, lease_live=%s)",
                    str(t["_id"]), ep, cur_epoch, lease_live)
                continue
            await db.scalp_owners.update_one(
                {"account_id": str(acc["_id"])},
                {"$max": {"max_order_epoch": ep}}, upsert=False)
        if t.get("scope") == "scalp_fast":
            # Phase A — formal lifecycle: the EA has claimed this order
            from scalp import order_state
            await order_state.apply(db, str(t["_id"]),
                                    order_state.EA_CLAIMED,
                                    f"claim:{str(t['_id'])}")
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

    resp = {"trades": out, "modifications": modifications}

    # iter-46 · Deep broker-history sync dispatch. When a sync is pending
    # (user-requested or auto-heal), tell the EA to re-scan its full deal
    # history for the lookback window. Re-dispatched every 180s until the EA
    # confirms via /bridge/sync-complete (covers dropped responses).
    pending_sync = acc.get("pending_history_sync")
    if pending_sync:
        dispatched = pending_sync.get("dispatched_at")
        redispatch = True
        if dispatched:
            try:
                redispatch = (datetime.now(timezone.utc)
                              - datetime.fromisoformat(dispatched)).total_seconds() > 180
            except ValueError:
                redispatch = True
        if redispatch:
            await db.accounts.update_one(
                {"_id": acc["_id"]},
                {"$set": {"pending_history_sync.dispatched_at":
                          datetime.now(timezone.utc).isoformat()}},
            )
            resp["sync_request"] = {
                "lookback_seconds": int(pending_sync.get("lookback_seconds") or 604800),
            }

    return resp


class BridgeSyncComplete(BaseModel):
    bridge_token: str
    deals_pushed: int = 0
    lookback_seconds: int | None = None


@router.post("/sync-complete")
async def sync_complete(payload: BridgeSyncComplete):
    """EA v1.39 confirms a deep broker-history sync finished.

    Clears the pending flag, records how many STOIC trade records were
    repaired (backfilled with exact broker figures) since the sync was
    requested, and pushes a live WS update to the user.
    """
    db = get_db()
    acc = await _account_by_token(payload.bridge_token)
    now_iso = datetime.now(timezone.utc).isoformat()
    pending = acc.get("pending_history_sync") or {}
    requested_at = pending.get("requested_at")
    repaired = 0
    if requested_at:
        repaired = await db.trades.count_documents({
            "account_id": str(acc["_id"]),
            "backfilled_at": {"$gte": requested_at},
        })
    await db.accounts.update_one({"_id": acc["_id"]}, {"$set": {
        "pending_history_sync": None,
        "last_full_sync_at": now_iso,
        "last_full_sync": {
            "completed_at": now_iso,
            "deals_pushed": payload.deals_pushed,
            "trades_repaired": repaired,
            "requested_by": pending.get("requested_by"),
            "lookback_seconds": payload.lookback_seconds or pending.get("lookback_seconds"),
        },
    }})
    await ws_manager.broadcast(acc["user_id"], "broker_sync_complete", {
        "account_id": str(acc["_id"]),
        "deals_pushed": payload.deals_pushed,
        "trades_repaired": repaired,
        "completed_at": now_iso,
    })
    logger.info(
        "Deep sync complete account=%s deals_pushed=%s repaired=%s",
        str(acc["_id"]), payload.deals_pushed, repaired,
    )
    return {"ok": True, "trades_repaired": repaired}


class BridgeModificationAck(BaseModel):
    bridge_token: str
    trade_id: str
    type: str   # MODIFY_SL | PARTIAL_CLOSE
    success: bool = True
    new_sl: float | None = None
    new_volume: float | None = None
    error: str | None = None
    intent_id: str | None = None   # EA v1.49+ echoes the command's intent


@router.post("/modification-ack")
async def modification_ack(payload: BridgeModificationAck):
    """EA acknowledges it applied a pending_modification on its end."""
    db = get_db()
    acc = await _account_by_token(payload.bridge_token)
    trade = await db.trades.find_one({"_id": ObjectId(payload.trade_id)})
    if not trade or trade["account_id"] != str(acc["_id"]):
        raise HTTPException(status_code=404, detail="Trade not found")

    # audit r3 P0 · sequence fence: a delayed/replayed ack for a SUPERSEDED
    # command must never clear the current one or apply an older stop.
    from command_fence import is_stale_ack
    if is_stale_ack(trade, payload.intent_id):
        logger.warning("stale modification-ack ignored trade=%s intent=%s",
                       payload.trade_id, payload.intent_id)
        return {"status": "stale_intent_ignored"}

    # Notifications to fire AFTER the EA confirms — collected here, dispatched
    # only on the success path so the user never gets a Telegram for an action
    # the broker never actually applied.
    pending_notifs: list = []

    update = {"pending_modification": None}
    # Round 11 item 9 — protection-ack policy lives in a dependency-free
    # function (protection_guard.apply_protection_ack) so it is unit-testable
    # without FastAPI/BSON/Mongo.
    from protection_guard import apply_protection_ack
    update.update(apply_protection_ack(
        trade, bool(payload.success),
        new_sl=(payload.new_sl if payload.type == "MODIFY_SL" else None),
        error=payload.error))
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
        # error + emergency-state transitions already applied above via
        # apply_protection_ack (round 11 item 9)
        pass

    await db.trades.update_one({"_id": ObjectId(payload.trade_id)}, {"$set": update})
    # Scalp fast path (round 18 review item 2): a MODIFY_SL ack promotes the
    # runner's PENDING stop to the CONFIRMED stop — until this ack the bot
    # must keep behaving as if the original stop is live at the broker.
    if trade.get("scope") == "scalp_fast" \
            and payload.type in ("MODIFY_SL", "PARTIAL_CLOSE"):
        try:
            from scalp.engine import runners_for_account
            for r in runners_for_account(str(acc["_id"])):
                if r.symbol != (trade.get("symbol") or "").upper():
                    continue
                if payload.type == "MODIFY_SL":
                    r.on_stop_modified(payload.trade_id, payload.new_sl,
                                       bool(payload.success), db=db)
                else:
                    # Phase B — adaptive partial acknowledged; the combo
                    # new_sl (if any) rode on the pending modification doc
                    r.on_partial_ack(
                        payload.trade_id, payload.new_volume,
                        bool(payload.success),
                        new_sl=(trade.get("pending_modification")
                                or {}).get("new_sl"), db=db)
        except Exception:
            pass
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

    # Idempotency guard (audit E2): a redispatched trade_id that the EA
    # ALREADY executed must never mint a second broker position. A second
    # OPEN report with a DIFFERENT ticket means a duplicate broker order —
    # flag for reconciliation instead of silently overwriting the mapping.
    if (payload.status == "open" and payload.mt5_ticket is not None
            and trade.get("mt5_ticket")
            and int(trade["mt5_ticket"]) != int(payload.mt5_ticket)):
        logger.critical(
            "DUPLICATE broker order detected trade=%s existing_ticket=%s "
            "new_ticket=%s account=%s — flagged requires_reconciliation",
            payload.trade_id, trade.get("mt5_ticket"), payload.mt5_ticket,
            str(acc["_id"]))
        await db.trades.update_one(
            {"_id": trade["_id"]},
            {"$set": {"duplicate_broker_tickets": sorted({
                          int(trade["mt5_ticket"]), int(payload.mt5_ticket)}),
                      "submission_state": "unknown_requires_reconciliation",
                      "requires_reconciliation": True}})
        return {"ok": True, "duplicate": True,
                "existing_ticket": trade.get("mt5_ticket")}

    update = {"status": payload.status,
              "submission_state": ("broker_accepted"
                                   if payload.status == "open"
                                   else f"broker_{payload.status}")}
    if payload.mt5_ticket is not None:
        update["mt5_ticket"] = payload.mt5_ticket
        update["acknowledged_at"] = datetime.now(timezone.utc).isoformat()

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
        action = trade.get("action")
        # iter-47 · TRUE slippage baseline. EA v1.40+ reports the price it
        # requested at OrderSend. The legacy baseline (signal price) conflated
        # 5-15s dispatch latency with broker slippage — 100% of historical
        # "vetoes" were false positives on trades that went on to profit.
        requested = float(payload.requested_price or 0)
        baseline = requested if requested > 0 else intended
        # Direction-aware: only an ADVERSE fill (worse entry) counts.
        # A favorable fill must never trigger the veto.
        if action == "SELL":
            adverse = baseline - actual
        else:
            adverse = actual - baseline
        slip_pips = price_to_pips(symbol, max(0.0, adverse)) if baseline > 0 else 0.0
        update["intended_entry_price"] = intended
        if requested > 0:
            update["requested_price"] = requested
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
        # Veto ONLY on a true-slippage measurement (requested_price present,
        # EA v1.40+). Legacy reports lack it and the signal-vs-fill delta is
        # dominated by latency drift, not execution quality.
        if cfg.get("slippage_veto_enabled", True) and requested > 0:
            caps = cfg.get("max_slippage_pips") or {"XAUUSD": 20.0, "BTCUSD": 80.0}
            base = base_symbol(symbol)
            cap = float(caps.get(base, caps.get(symbol, caps.get(symbol.upper(), 9999))))
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
        update["pending_modification"] = None  # closed — queue entry is moot
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
    # Scalp fast-path reconciliation (EA v1.44). /bridge/report is the
    # OPERATIONAL acknowledgement path only: it feeds fill confirmation and
    # frees the position slot. Financial reconciliation (P&L, commission,
    # swap) is applied EXCLUSIVELY by /bridge/external-deal, which carries
    # the authoritative signed MT5 deal fields (round 5 item 1).
    if trade.get("scope") == "scalp_fast":
        try:
            from scalp.engine import runners_for_account
            from scalp import order_state
            for r in runners_for_account(str(acc["_id"])):
                if r.symbol != (trade.get("symbol") or "").upper():
                    continue
                if payload.status == "open" and payload.entry_price is not None:
                    # Phase A recovery — adopt fills for unknown trades
                    if payload.trade_id not in r.live_trades:
                        r.adopt_open_trade(trade)
                    await r.on_trade_opened(payload.trade_id,
                                      float(trade.get("entry_price") or 0) or None,
                                      float(payload.entry_price), db=db)
                    # round 18 review item 1 — reservation release now
                    # happens INSIDE on_trade_opened (awaited, idempotent).
                    # P0-1 · protection-aware lifecycle: the fill ack alone
                    # does NOT prove the stop is live. BROKER_ACCEPTED →
                    # FILLED_UNPROTECTED here; the next heartbeat position
                    # snapshot (which carries the broker's actual SL) flips
                    # it to PROTECTED → OPEN, or triggers a re-arm/close.
                    await order_state.apply(db, payload.trade_id,
                                            order_state.BROKER_ACCEPTED,
                                            f"ack:{payload.trade_id}")
                    await order_state.apply(db, payload.trade_id,
                                            order_state.FILLED_UNPROTECTED,
                                            f"filled:{payload.trade_id}")
                    await db.trades.update_one(
                        {"_id": ObjectId(payload.trade_id)},
                        {"$set": {"protection": {
                            "state": "AWAITING_CONFIRM",
                            "filled_at": datetime.now(timezone.utc).isoformat(),
                            "requested_sl": float(trade.get("stop_loss") or 0),
                        }}})
                elif payload.status == "closed":
                    await r.on_close_ack(payload.trade_id,
                                   exit_price=(float(payload.exit_price)
                                               if payload.exit_price else None),
                                   db=db)
                    await order_state.apply(db, payload.trade_id,
                                            order_state.CLOSED,
                                            f"closed:{payload.trade_id}")
        except Exception:
            pass
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


async def _mark_deal_reconciled(db, deal_id, account_id: str, note: str | None = None):
    doc = {"financial_reconciliation_status": "complete",
           "financial_reconciled_at": datetime.now(timezone.utc).isoformat()}
    if note:
        doc["reconciliation_note"] = note
    # Round 13 item 7 — completion is guarded: a deal already marked
    # complete is never re-stamped by a racing reconciliation path.
    await db.broker_deals.update_one(
        {"deal_id": deal_id, "account_id": account_id,
         "financial_reconciliation_status": {"$ne": "complete"}},
        {"$set": doc})


async def _scalp_reconcile_close(db, account_id: str, trade: dict, payload,
                                 partial: bool = False,
                                 remaining_lots: float | None = None,
                                 occurred_at_iso: str | None = None):
    """Round 7 critical item — a broker deal flips to 'complete' ONLY after
    engine.apply_broker_deal confirms the runner applied it (the runner is
    constructed + restored on demand). On failure the deal STAYS pending
    with reconciliation_error, so the recovery sweep retries it."""
    from scalp.engine import apply_broker_deal
    if occurred_at_iso is None:
        row = await db.broker_deals.find_one(
            {"deal_id": payload.deal_id, "account_id": account_id},
            {"occurred_at": 1, "received_at": 1})
        occurred_at_iso = ((row or {}).get("occurred_at")
                           or (row or {}).get("received_at"))
    res = await apply_broker_deal(
        db, account_id, trade,
        deal_id=payload.deal_id, lots=payload.lots or 0,
        profit=payload.profit or 0, commission=payload.commission or 0,
        swap=payload.swap or 0, price=payload.price,
        partial=partial, remaining_lots=remaining_lots,
        occurred_at_iso=occurred_at_iso)
    if res["applied"]:
        await _mark_deal_reconciled(db, payload.deal_id, account_id)
    else:
        logger.warning("scalp reconciliation NOT applied deal=%s: %s",
                       payload.deal_id, res["reason"])
        await db.broker_deals.update_one(
            {"deal_id": payload.deal_id, "account_id": account_id},
            {"$set": {"reconciliation_error": res["reason"],
                      "reconciliation_target": "scalp_runner"},
             "$inc": {"reconciliation_attempts": 1}})
    return res


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
        "position_volume": payload.position_volume,
        # Round 6 — durable financial-reconciliation state: close deals stay
        # 'pending' until the runner/trade application completes; a recovery
        # sweep resumes any deal stranded by a crash in between.
        "financial_reconciliation_status": ("pending" if payload.deal_entry != "in"
                                            else "none"),
        "financial_reconciled_at": None,
        "received_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        await db.broker_deals.insert_one(deal_doc)
    except DuplicateKeyError:
        # Already processed this (deal_id, account_id). Normally a no-op —
        # BUT (iter-46) a deep-sync re-push must still be able to REPAIR a
        # trade record that carries estimated/missing exit data. Only fall
        # through for "out" deals whose matched trade is CLOSED and still
        # inexact; everything else stays a safe no-op (an open trade must
        # never be re-processed — the partial-close math would double-fire).
        if payload.deal_entry == "in":
            return {"ok": True, "duplicate": True, "deal_id": payload.deal_id}
        t = await db.trades.find_one({
            "account_id": account_id, "mt5_ticket": payload.mt5_ticket,
        })
        # Round 6 — RESUME interrupted scalp financial reconciliation: the
        # deal row exists but the process may have died before the runner
        # received it. Runner deal-id idempotency makes this replay safe.
        deal_row = await db.broker_deals.find_one(
            {"deal_id": payload.deal_id, "account_id": account_id})
        if (t is not None and t.get("scope") == "scalp_fast"
                and deal_row is not None
                and deal_row.get("financial_reconciliation_status") == "pending"):
            await _scalp_reconcile_close(db, account_id, t, payload,
                                         partial=(t.get("status") == "open"))
            return {"ok": True, "duplicate": True, "deal_id": payload.deal_id,
                    "reconciliation_resumed": True}
        needs_repair = (
            t is not None and t.get("status") == "closed"
            and (t.get("pnl_estimated") or t.get("pnl_unknown")
                 or t.get("exit_price") is None)
        )
        if not needs_repair:
            return {"ok": True, "duplicate": True, "deal_id": payload.deal_id}
        logger.info(
            "Duplicate deal %s re-processed to repair inexact trade %s (ticket %s)",
            payload.deal_id, t["_id"], payload.mt5_ticket,
        )
    except Exception as e:
        # Real failure — surface so the EA can retry. Silently swallowing here
        # would lose broker fills.
        logger.exception("broker_deals.insert_one failed deal_id=%s acct=%s: %s",
                         payload.deal_id, account_id, e)
        raise HTTPException(status_code=500, detail="Failed to persist broker deal")

    # STOIC uses SERVER-RECEIVED UTC as the canonical timestamp on trade
    # docs (opened_at / closed_at / partial_closed_at). MT5's DEAL_TIME is
    # broker-server-LOCAL seconds since epoch — NOT true UTC. Different
    # brokers sit in different timezones (OnEquity UTC+3, IC Markets UTC+2,
    # etc.) and MT5 gives no timezone metadata alongside the raw epoch. If
    # we treat that as UTC via `datetime.fromtimestamp(..., tz=utc)`, the
    # resulting ISO string is off by the broker's timezone offset — which
    # causes closed_at (real UTC now) to appear BEFORE opened_at (broker
    # local labelled as UTC). Users see impossible timestamp inversions.
    #
    # Instead: use the receiving-server's real UTC time for the canonical
    # field and keep the raw broker epoch in a sidecar field so we can
    # still audit against broker records.
    server_iso = datetime.now(timezone.utc).isoformat()
    deal_iso = server_iso
    broker_deal_epoch = payload.deal_time or None
    # iter-48 · HISTORICAL deals (deep-sync / history-sweep backfills) must
    # keep the broker's own event time — stamping them with "now" folds
    # trades that closed days ago into TODAY's stats (wrong win-rate/P&L).
    # iter-51 · Broker epochs are broker-LOCAL (typically UTC+2/+3 EET). We
    # LEARN each account's UTC offset from live deals (broker epoch vs server
    # receive time, snapped to 15 min) and subtract it from historical epochs
    # so backfilled closes land on the correct UTC calendar day. Live events
    # keep the accurate server-UTC stamp.
    if broker_deal_epoch:
        try:
            now_dt = datetime.now(timezone.utc)
            deal_dt = datetime.fromtimestamp(int(broker_deal_epoch), tz=timezone.utc)
            age_sec = (now_dt - deal_dt).total_seconds()
            if payload.backfill or age_sec > 600:
                # Round 8 item 8 — use the offset that was VALID AT the deal
                # timestamp (offset history), not just the current one; DST
                # transitions and broker server-time changes stay correct.
                offset = int(acc.get("broker_utc_offset_sec") or 0)
                hist = await db.broker_time_offsets.find_one(
                    {"account_id": account_id,
                     "effective_from": {"$lte": deal_dt.isoformat()}},
                    sort=[("effective_from", -1)])
                if hist is not None:
                    offset = int(hist.get("offset_seconds") or offset)
                deal_iso = datetime.fromtimestamp(
                    int(broker_deal_epoch) - offset, tz=timezone.utc
                ).isoformat()
            else:
                # Live deal → learn/refresh the broker's UTC offset.
                raw_off = int(broker_deal_epoch) - int(now_dt.timestamp())
                snapped = int(round(raw_off / 900.0) * 900)
                if abs(snapped) <= 50400 and snapped != int(acc.get("broker_utc_offset_sec") or 0):
                    await db.accounts.update_one(
                        {"_id": acc["_id"]},
                        {"$set": {"broker_utc_offset_sec": snapped}},
                    )
                    # persist offset history with effective dates (round 8)
                    await db.broker_time_offsets.insert_one({
                        "account_id": account_id,
                        "broker_server": acc.get("broker_server") or acc.get("broker"),
                        "offset_seconds": snapped,
                        "effective_from": now_dt.isoformat(),
                    })
        except (ValueError, OSError, OverflowError):
            pass

    # Round 11 item 2 — stamp the normalized ECONOMIC event time onto the
    # broker-deal row so ledger events and recovery always attribute the
    # P&L to the correct calendar day (occurred_at vs received_at).
    try:
        await db.broker_deals.update_one(
            {"deal_id": payload.deal_id, "account_id": account_id},
            {"$set": {"occurred_at": deal_iso}})
    except Exception:  # noqa: BLE001 — annotation only, never blocks the deal
        pass

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

        # iter-93: race-condition fix. When STOIC fires an order, execution.py
        # inserts a trade doc with status="pending" and mt5_ticket=None. The EA
        # then opens the position and issues TWO callbacks:
        #   1) /bridge/report  → attaches mt5_ticket to the pending doc
        #   2) /bridge/external-deal (this handler) → OnTradeTransaction "in"
        # If (2) arrives before (1), the lookup above misses (no matching
        # ticket yet) and we fall into the fresh-insert path below — which
        # hardcodes stop_loss=0.0 and take_profit=0.0 because BridgeExternalDeal
        # doesn't carry SL/TP fields. Result: a duplicate trade with NO stops,
        # invisible to Safety Guardian's risk math and unclosable via the
        # normal SL/TP mechanism. Adopt any STOIC-owned pending sibling
        # instead of orphaning it, so the SL/TP set by the signal survive.
        if payload.magic == STOIC_MAGIC:
            twelve_min_ago = (datetime.now(timezone.utc) - timedelta(minutes=12)).isoformat()
            adopt_q = {
                "account_id": account_id,
                "symbol": payload.symbol,
                "action": payload.action,
                "status": "pending",
                "mt5_ticket": None,
                "opened_at": {"$gte": twelve_min_ago},
            }
            sibling = await db.trades.find_one(adopt_q, sort=[("opened_at", -1)])
            if sibling:
                await db.trades.update_one(
                    {"_id": sibling["_id"]},
                    {"$set": {
                        "mt5_ticket": payload.mt5_ticket,
                        "status": "open",
                        "entry_price": payload.price,
                        "adopted_via_external_deal": True,
                        "adopted_at": datetime.now(timezone.utc).isoformat(),
                    }},
                )
                await ws_manager.broadcast(user_id, "trade_updated", {
                    "trade_id": str(sibling["_id"]),
                    "status": "open",
                    "external_open": False,
                    "symbol": payload.symbol,
                    "action": payload.action,
                })
                return {"ok": True, "adopted": str(sibling["_id"])}

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
            "broker_deal_epoch": broker_deal_epoch,
            "closed_at": None,
            "origin": trade_origin,
            "magic_number": int(payload.magic or 0),
            "external_open": is_external,
        }
        # Round 7 item 9 — a bot-owned open with NO pending sibling means we
        # lost the order context: this record carries zero SL/TP, invisible
        # to stop-risk math. Flag it so Safety Guardian / users see it needs
        # protection instead of silently running unstopped.
        protection_unknown = False
        if payload.magic == STOIC_MAGIC:
            trade_doc["protection_missing"] = True
            trade_doc["protection_state"] = "PROTECTION_UNKNOWN"
            # round 9 item 1 — IMMEDIATE remediation, not next-sweep:
            from scalp.engine import set_protection_block
            set_protection_block(account_id, True)
            protection_unknown = True
            logger.warning(
                "bot-owned 'in' deal %s (ticket %s) had no pending sibling — "
                "trade created with protection_missing=True",
                payload.deal_id, payload.mt5_ticket)
        result = await db.trades.insert_one(trade_doc)
        if protection_unknown:
            # fire the recovery state machine NOW — an unprotected bot
            # position must not wait for the next scheduled sweep
            import asyncio as _aio
            from protection_guard import repair_unprotected_positions
            _aio.create_task(repair_unprotected_positions(db))
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
    remaining_lots = 0.0
    if existing and existing.get("status") == "open":
        # Round 7 items 3/4 — broker position_volume is authoritative but a
        # sub-minimum rounding residual must NOT count as an open position,
        # and broker-reported volume is NEVER inflated to the minimum lot.
        # Round 8 item 5 — use the instrument's real volume spec when known.
        from scalp.deals import classify_close
        from scalp.instruments import approved as _scalp_approved
        cfg = _scalp_approved((existing.get("symbol") or "").upper())
        is_partial_close, remaining_lots = classify_close(
            payload.position_volume,
            float(existing.get("lot_size") or 0),
            float(payload.lots or 0),
            min_lot=cfg.min_lot if cfg else 0.01,
            lot_step=cfg.lot_step if cfg else 0.01)

    if is_partial_close:
        new_lot = round(remaining_lots, 3)
        update = {
            # broker volume stored EXACTLY (round 7 item 3) — classify_close
            # already guarantees new_lot >= min_lot when broker-reported;
            # only the legacy lot-arithmetic fallback needs the floor.
            "lot_size": (new_lot if payload.position_volume is not None
                         else max(new_lot, 0.01)),
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
        # Round 6 item 1 — partial scalp closes hit the SAME financial
        # budgets as full closes (realized P&L, signed costs, lot shrink).
        if existing.get("scope") == "scalp_fast":
            await _scalp_reconcile_close(
                db, account_id,
                {**existing, "lot_size": update["lot_size"], "status": "open"},
                payload, partial=True, remaining_lots=update["lot_size"],
                occurred_at_iso=deal_iso)
        else:
            await _mark_deal_reconciled(db, payload.deal_id, account_id,
                                        note="not_scalp_scope")
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
        "pending_modification": None,  # closed — any queued mod is moot
    }
    if existing:
        real_exit_known = (existing.get("exit_price") is not None
                           and not existing.get("pnl_estimated"))
        if real_exit_known:
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
            update["pnl_unknown"] = False   # real P&L recovered
            update["pnl_estimated"] = False  # exact broker figures now
            # Distinguish "auto-repaired from reconciler ghost" vs "first-time
            # close report" — useful in the Audit Trail when STOIC initially
            # had no exit_price and the EA later caught up.
            if existing.get("status") == "closed":
                update["auto_repaired_from_ghost"] = True
                update["auto_repaired_at"] = datetime.now(timezone.utc).isoformat()
        await db.trades.update_one({"_id": existing["_id"]}, {"$set": update})
        tid = str(existing["_id"])
        # Scalp fast-path AUTHORITATIVE financial reconciliation (round 5
        # item 1 / round 6 durable state): SIGNED profit/commission/swap +
        # exact exit price, idempotent per deal_id, marks the broker deal
        # 'complete' only after the runner applied it.
        if existing.get("scope") == "scalp_fast":
            await _scalp_reconcile_close(
                db, account_id, {**existing, **update, "_id": existing["_id"]},
                payload, partial=False, occurred_at_iso=deal_iso)
        else:
            await _mark_deal_reconciled(db, payload.deal_id, account_id,
                                        note="not_scalp_scope")
        # Round 7 item 8 — netting-account REVERSAL: an "inout" deal closes
        # the tracked direction AND opens the opposite one in a single deal.
        # Track the remaining broker position so STOIC never runs blind, and
        # flag it protection_missing (no SL/TP known yet).
        if (payload.deal_entry == "inout" and payload.position_volume
                and float(payload.position_volume) >= 0.005):
            was_scalp = existing.get("scope") == "scalp_fast"
            rev_doc = {
                "user_id": user_id, "account_id": account_id,
                "symbol": payload.symbol, "action": payload.action,
                "lot_size": round(float(payload.position_volume), 3),
                "entry_price": payload.price,
                "stop_loss": 0.0, "take_profit": 0.0,
                "exit_price": None, "pnl": 0.0, "status": "open",
                "mode": "live", "broker": acc.get("broker", "MT5"),
                "mt5_ticket": payload.mt5_ticket,
                "opened_at": deal_iso, "closed_at": None,
                "origin": trade_origin, "magic_number": int(payload.magic or 0),
                "external_open": True, "reversal_open": True,
                "protection_missing": True,
                "protection_state": "PROTECTION_UNKNOWN",
            }
            if was_scalp:
                # Round 8 item 4 — scalp strategies do NOT support reversals:
                # flatten the unexpected opposite position immediately.
                rev_doc.update({
                    "close_requested": True,
                    "close_reason": "unexpected_reversal",
                    "protection_state": "EMERGENCY_CLOSE_PENDING",
                    "pending_modification": {
                        "type": "FULL_CLOSE",
                        "reason": "unexpected_scalp_reversal",
                        "requested_at": datetime.now(timezone.utc).isoformat()},
                })
            rev = await db.trades.insert_one(rev_doc)
            logger.warning(
                "inout REVERSAL on ticket %s: closed tracked side, broker "
                "still holds %.2f lots %s — %s (trade %s)",
                payload.mt5_ticket, float(payload.position_volume),
                payload.action,
                "EMERGENCY CLOSE queued (scalp)" if was_scalp
                else "tracked with protection_missing=True",
                str(rev.inserted_id))
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
        await _mark_deal_reconciled(db, payload.deal_id, account_id,
                                    note="no_tracked_trade_audit_row")

    await ws_manager.broadcast(user_id, "trade_updated", {
        "trade_id": tid, **update,
    })

    # iter-127c · auto post-mortem on EVERY losing close, including
    # broker-reported / external closes that bypass execution.py
    if realized is not None and float(realized) < 0 and update.get("status", "closed") == "closed":
        try:
            import asyncio as _aio
            from loss_postmortem import maybe_record_postmortem
            _aio.create_task(maybe_record_postmortem(db, tid))
        except Exception:
            pass

    if existing and existing.get("exit_price") is None:
        try:
            from notifier import notify_trade_closed
            full = await db.trades.find_one({"_id": ObjectId(tid)})
            if full:
                await notify_trade_closed(user_id, full)
        except Exception:
            pass

    return {"ok": True, "updated": tid, "pnl": realized}
