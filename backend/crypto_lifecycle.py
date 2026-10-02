"""Crypto position lifecycle reconciliation (ccxt venues).

Crypto trades are written by crypto_bridge/binance_engine.py with
`broker_kind="binance"` (the legacy marker reused for every ccxt venue),
`exchange_symbol`, `exchange_order_id`, `client_order_id` and a
`protection` sub-document (crypto_bridge/protective.py). Nothing on the
MT5 side (trade_manager, bridge acks, heartbeat reconciler) ever touches
them, so this sweep is their single source of lifecycle truth:

  pending → open      entry order filled on the exchange (fill price/size),
                      then exchange-side SL/TP placed (or flattened)
  pending → cancelled entry cancelled/expired/rejected with zero fill, or a
                      lost-response entry that never reached the exchange
  open    → open      protection missing / failed → (re)place or flatten
  open    → closed    SL leg filled (close_reason "sl"), TP leg filled
                      ("tp"), soft-TP crossed ("tp"), position closed
                      outside STOIC ("external_close")

Every write is status-guarded (`{"_id": …, "status": <expected>}`) so the
sweep is idempotent and safe against concurrent workers / manual closes.
It never frees capital, touches accounts or other trades.

Wiring: `run_loop()` is a perpetual coroutine (see the coordinator note in
the module report); `sweep_once(db)` is the unit of work.
"""
from __future__ import annotations

import asyncio
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from crypto_bridge.ccxt_engine import CCXTClient
from crypto_bridge.protective import (
    compute_pnl, flatten_and_close, order_fill_price,
    order_status, protect_amount, protect_or_flatten,
)

logger = logging.getLogger("crypto.lifecycle")

CRYPTO_BROKER_KIND = "binance"
ACTIVE_STATUSES = ["pending", "open"]
_DONE = ("canceled", "cancelled", "expired", "rejected")
# A lost-response entry is only declared "never placed" after this grace.
ENTRY_UNKNOWN_GRACE_S = int(os.environ.get("CRYPTO_ENTRY_UNKNOWN_GRACE_SEC", "120"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _age_s(iso: Optional[str]) -> float:
    try:
        ts = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:  # noqa: BLE001
        return 0.0


def _oid(v):
    from bson import ObjectId
    try:
        return ObjectId(str(v))
    except Exception:  # noqa: BLE001
        return v


def _is_not_found(exc: BaseException) -> bool:
    try:
        import ccxt.async_support as _ccxt  # type: ignore[import]
        if isinstance(exc, _ccxt.OrderNotFound):
            return True
    except Exception:  # noqa: BLE001
        pass
    return "notfound" in type(exc).__name__.lower()


async def _intent(db, trade: dict, to: str, detail: str) -> None:
    iid = trade.get("execution_intent_id")
    if not iid:
        return
    try:
        from execution_intents import transition
        await transition(db, iid, to, detail=detail,
                         result={"trade_id": str(trade.get("_id"))})
    except Exception as e:  # noqa: BLE001
        logger.warning("intent %s → %s failed: %s", iid, to, e)


async def _notify_closed(db, trade: dict, fields: dict) -> None:
    try:
        from ws_manager import manager as ws_manager
        await ws_manager.broadcast(trade.get("user_id"), "trade_updated",
                                   {"trade_id": str(trade["_id"]), **fields})
    except Exception as e:  # noqa: BLE001
        logger.debug("ws broadcast failed: %s", e)
    try:
        from notifier import notify_trade_closed
        await notify_trade_closed(trade.get("user_id"), {**trade, **fields})
    except Exception as e:  # noqa: BLE001
        logger.debug("close notification failed: %s", e)


async def _close(db, trade: dict, *, exit_price: float, reason: str,
                 exit_order: Optional[dict] = None, stats: dict) -> bool:
    """open → closed (status-guarded). Returns True if THIS call closed it."""
    prot = dict(trade.get("protection") or {})
    amount = float(prot.get("amount") or trade.get("lot_size") or 0)
    pnl = compute_pnl(trade, exit_price, amount, exit_order)
    now = _now()
    prot.update({"status": "done", "updated_at": now})
    fields = {"status": "closed", "exit_price": round(float(exit_price), 8),
              "pnl": pnl, "closed_at": now, "close_reason": reason,
              "exit_order_id": str((exit_order or {}).get("id") or "") or None,
              "protection": prot}
    r = await db.trades.update_one({"_id": trade["_id"], "status": "open"},
                                   {"$set": fields})
    if not getattr(r, "modified_count", 0):
        return False
    stats["closed"] += 1
    stats.setdefault("closed_by_reason", {}).setdefault(reason, 0)
    stats["closed_by_reason"][reason] += 1
    logger.info("crypto trade %s closed (%s) exit=%s pnl=%s", trade["_id"],
                reason, exit_price, pnl)
    await _notify_closed(db, trade, {k: fields[k] for k in
                                     ("status", "exit_price", "pnl", "close_reason")})
    return True


async def _safe_cancel(client, leg: Optional[dict], symbol: str) -> bool:
    if not leg or not leg.get("order_id"):
        return True
    try:
        await client.cancel_order(leg["order_id"], symbol, leg.get("params") or {})
        return True
    except Exception as e:  # noqa: BLE001 — already gone is fine
        if _is_not_found(e):
            return True
        logger.warning("cancel of sibling order %s failed: %s", leg.get("order_id"), e)
        return False


# ───────────────────────────── entry (pending) ─────────────────────────────

async def _reconcile_entry(db, client, account: dict, trade: dict, stats: dict) -> None:
    sym = trade["exchange_symbol"]
    order = None
    if trade.get("exchange_order_id"):
        order = await client.fetch_order(trade["exchange_order_id"], sym)
    elif trade.get("client_order_id"):
        try:
            order = await client.fetch_order_by_client_id(trade["client_order_id"], sym)
        except Exception as e:  # noqa: BLE001
            if _is_not_found(e) and _age_s(trade.get("opened_at")) > ENTRY_UNKNOWN_GRACE_S:
                r = await db.trades.update_one(
                    {"_id": trade["_id"], "status": "pending"},
                    {"$set": {"status": "cancelled", "closed_at": _now(),
                              "close_reason": "entry_not_found",
                              "exchange_order_status": "not_found"}})
                if getattr(r, "modified_count", 0):
                    stats["cancelled"] += 1
                    await _intent(db, trade, "failed_confirmed",
                                  "exchange truth: entry order not found")
                return
            raise
    if not order:
        return
    st = order_status(order)
    filled = float(order.get("filled") or 0)
    if st == "closed" or (st in _DONE and filled > 0):
        amount = filled or float(trade.get("lot_size") or 0)
        fill_px = order_fill_price(order, fallback=float(trade.get("entry_price") or 0))
        sets = {"status": "open", "entry_price": round(fill_px, 8),
                "lot_size": amount, "filled_at": _now(),
                "exchange_order_id": str(order.get("id") or trade.get("exchange_order_id") or ""),
                "exchange_order_status": order.get("status"),
                "protection": {"status": "pending"}}
        r = await db.trades.update_one({"_id": trade["_id"], "status": "pending"},
                                       {"$set": sets})
        if not getattr(r, "modified_count", 0):
            return
        stats["filled"] += 1
        await _intent(db, trade, "filled", f"entry filled @ {fill_px}")
        trade.update(sets)
        res = await protect_or_flatten(db, client, account, trade,
                                       amount=protect_amount(trade, order))
        stats[res["outcome"]] = stats.get(res["outcome"], 0) + 1
    elif st in _DONE:
        r = await db.trades.update_one(
            {"_id": trade["_id"], "status": "pending"},
            {"$set": {"status": "cancelled", "closed_at": _now(),
                      "close_reason": "entry_" + ("cancelled" if st.startswith("cancel") else st),
                      "exchange_order_status": order.get("status")}})
        if getattr(r, "modified_count", 0):
            stats["cancelled"] += 1
            await _intent(db, trade, "cancelled", f"entry {st} with no fill")
    elif not trade.get("exchange_order_id") and order.get("id"):
        # lost-response entry resolved: it IS resting on the exchange
        await db.trades.update_one(
            {"_id": trade["_id"], "status": "pending"},
            {"$set": {"exchange_order_id": str(order["id"]),
                      "exchange_order_status": order.get("status"),
                      "entry_uncertain": None}})


# ─────────────────────────── protection (open) ───────────────────────────

async def _position_gone(client, trade: dict, prot: dict) -> bool:
    """Best-effort 'closed outside STOIC' check after a protective order
    vanished. Spot BUY: base-asset balance below half the protected size.
    SELL/contract positions are never inferred closed (re-protect instead)."""
    if str(trade.get("action") or "").upper() != "BUY" or prot.get("contract"):
        return False
    base = str(trade.get("exchange_symbol") or "").split("/")[0]
    try:
        bal = await client.fetch_balance()
    except Exception:  # noqa: BLE001 — unknown ≠ gone
        return False
    total = float(((bal or {}).get("total") or {}).get(base) or 0)
    need = float(prot.get("amount") or trade.get("lot_size") or 0)
    return need > 0 and total < need * 0.5


async def _last_price(client, symbol: str) -> float:
    try:
        t = await client.fetch_ticker(symbol)
        return float(t.get("last") or t.get("close") or 0)
    except Exception:  # noqa: BLE001
        return 0.0


async def _reconcile_open(db, client, account: dict, trade: dict, stats: dict) -> None:
    sym = trade["exchange_symbol"]
    prot = trade.get("protection") or {}
    if prot.get("status") != "placed" or not (prot.get("sl") or {}).get("order_id"):
        # never protected, crashed mid-placement, or a failed flatten → retry
        amount = float(prot.get("amount") or 0) or protect_amount(trade)
        res = await protect_or_flatten(db, client, account, trade, amount=amount)
        stats[res["outcome"]] = stats.get(res["outcome"], 0) + 1
        return

    sl_leg, tp_leg = prot.get("sl") or {}, prot.get("tp") or None
    oco = bool(prot.get("oco"))

    if prot.get("single_order"):  # OKX algo OCO: one order, two triggers
        o = await client.fetch_order(sl_leg["order_id"], sym, sl_leg.get("params") or {})
        st = order_status(o)
        if st == "closed":
            side = str(((o or {}).get("info") or {}).get("actualSide") or "").lower()
            px = order_fill_price(o, 0.0)
            if side not in ("sl", "tp"):
                sl_p, tp_p = float(sl_leg.get("price") or 0), float((tp_leg or {}).get("price") or 0)
                ref = px or await _last_price(client, sym)
                side = "tp" if tp_p and abs(ref - tp_p) < abs(ref - sl_p) else "sl"
            if not px:
                px = float((tp_leg if side == "tp" else sl_leg).get("price") or 0)
            await _close(db, trade, exit_price=px, reason=side, exit_order=o, stats=stats)
        elif st in _DONE:
            await _handle_vanished_stop(db, client, account, trade, prot, stats)
        return

    sl_o = await client.fetch_order(sl_leg["order_id"], sym, sl_leg.get("params") or {})
    tp_o = None
    if tp_leg and tp_leg.get("order_id"):
        tp_o = await client.fetch_order(tp_leg["order_id"], sym, tp_leg.get("params") or {})
    sl_st, tp_st = order_status(sl_o), order_status(tp_o)

    if sl_st == "closed":
        px = order_fill_price(sl_o, float(sl_leg.get("price") or 0))
        if await _close(db, trade, exit_price=px, reason="sl", exit_order=sl_o, stats=stats):
            if not oco and tp_st == "open":
                await _safe_cancel(client, tp_leg, sym)
        return
    if tp_st == "closed":
        px = order_fill_price(tp_o, float(tp_leg.get("price") or 0))
        if await _close(db, trade, exit_price=px, reason="tp", exit_order=tp_o, stats=stats):
            if not oco and sl_st == "open":
                if await _safe_cancel(client, sl_leg, sym):
                    stats["siblings_cancelled"] += 1
        return
    if sl_st in _DONE:
        await _handle_vanished_stop(db, client, account, trade, prot, stats,
                                    tp_leg=tp_leg if tp_st == "open" else None)
        return
    # both resting — soft TP enforcement when the TP limit was rejected
    tp_price = float(trade.get("take_profit") or 0)
    if prot.get("soft_tp") and tp_price > 0 and sl_st == "open":
        last = await _last_price(client, sym)
        buy = str(trade.get("action") or "").upper() == "BUY"
        if last > 0 and ((buy and last >= tp_price) or (not buy and last <= tp_price)):
            if not await _safe_cancel(client, sl_leg, sym):
                return  # stop may have just filled — next sweep decides
            try:
                chk = await client.fetch_order(sl_leg["order_id"], sym, sl_leg.get("params") or {})
                if order_status(chk) == "closed":
                    px = order_fill_price(chk, float(sl_leg.get("price") or 0))
                    await _close(db, trade, exit_price=px, reason="sl", exit_order=chk, stats=stats)
                    return
            except Exception:  # noqa: BLE001
                pass
            res = await flatten_and_close(db, client, account, trade,
                                          float(prot.get("amount") or trade.get("lot_size") or 0),
                                          reason_detail="soft take-profit crossed",
                                          close_reason="tp")
            if res["outcome"] == "flattened":
                stats["closed"] += 1
            else:
                # stop already cancelled and flatten failed: re-protect next pass
                await db.trades.update_one(
                    {"_id": trade["_id"], "status": "open"},
                    {"$set": {"protection.status": "flatten_failed"}})


async def _handle_vanished_stop(db, client, account, trade, prot, stats, tp_leg=None) -> None:
    """The stop is gone without filling (cancelled on the exchange UI,
    expired, …). Either the position was closed outside STOIC, or it is
    now naked and must be re-protected immediately."""
    sym = trade["exchange_symbol"]
    if await _position_gone(client, trade, prot):
        await _safe_cancel(client, tp_leg, sym)
        px = await _last_price(client, sym) or float(trade.get("entry_price") or 0)
        await _close(db, trade, exit_price=px, reason="external_close", stats=stats)
        return
    # still exposed → release the TP lock (spot) and re-protect now
    if tp_leg and not prot.get("oco"):
        await _safe_cancel(client, tp_leg, sym)
    logger.warning("crypto trade %s stop vanished on exchange — re-protecting", trade["_id"])
    trade = dict(trade)
    # fresh deterministic ids would collide with the old ones on venues that
    # remember clientOrderIds; suffix the seed with the attempt counter.
    n = int(prot.get("reprotect_count") or 0) + 1
    trade["client_order_id"] = f"{(trade.get('client_order_id') or 'stoic')[:27]}r{n}"
    res = await protect_or_flatten(db, client, account, trade,
                                   amount=float(prot.get("amount") or 0) or protect_amount(trade))
    if res["outcome"] == "protected":
        await db.trades.update_one({"_id": trade["_id"], "status": "open"},
                                   {"$set": {"protection.reprotect_count": n}})
    stats[res["outcome"]] = stats.get(res["outcome"], 0) + 1


# ─────────────────────────────── sweep ───────────────────────────────

async def _reconcile_trade(db, client, account, trade, stats) -> None:
    if trade.get("status") == "pending":
        await _reconcile_entry(db, client, account, trade, stats)
    elif trade.get("status") == "open":
        await _reconcile_open(db, client, account, trade, stats)


async def sweep_once(db, *, client_factory=None, limit: int = 1000) -> dict:
    """One idempotent pass over every pending/open crypto trade."""
    factory = client_factory or CCXTClient
    stats = defaultdict(int)
    stats["closed_by_reason"] = {}
    trades = await db.trades.find(
        {"broker_kind": CRYPTO_BROKER_KIND, "status": {"$in": ACTIVE_STATUSES}}
    ).to_list(length=limit)
    by_account: dict = defaultdict(list)
    for t in trades:
        if t.get("exchange_symbol"):
            by_account[str(t.get("account_id") or "")].append(t)
    for acct_id, items in by_account.items():
        account = await db.accounts.find_one({"_id": _oid(acct_id)}) if acct_id else None
        if not account:
            # never infer a close without the venue — just surface it
            logger.error("crypto lifecycle: account %s missing for %d active trade(s)",
                         acct_id, len(items))
            stats["account_missing"] += len(items)
            continue
        try:
            async with factory(account) as client:
                for t in items:
                    stats["checked"] += 1
                    try:
                        await _reconcile_trade(db, client, account, t, stats)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:  # noqa: BLE001 — one trade never stops the sweep
                        stats["errors"] += 1
                        logger.warning("crypto lifecycle trade %s failed: %s",
                                       t.get("_id"), e)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — client construction (creds, …)
            stats["errors"] += len(items)
            logger.warning("crypto lifecycle account %s failed: %s", acct_id, e)
    out = dict(stats)
    out["closed_by_reason"] = dict(stats["closed_by_reason"])
    for k in ("checked", "filled", "closed", "cancelled", "errors", "siblings_cancelled"):
        out.setdefault(k, 0)
    return out


async def run_loop(stop_event: Optional[asyncio.Event] = None,
                   interval_s: Optional[float] = None) -> None:
    """Perpetual sweep. Interval: CRYPTO_LIFECYCLE_INTERVAL_SEC (default 15s)."""
    from database import get_db
    interval = float(interval_s if interval_s is not None
                     else os.environ.get("CRYPTO_LIFECYCLE_INTERVAL_SEC", "15"))
    while not (stop_event is not None and stop_event.is_set()):
        t0 = datetime.now(timezone.utc)
        try:
            res = await sweep_once(get_db())
            try:
                from workers.base import record_progress
                record_progress("crypto_lifecycle_loop",
                                processed=int(res.get("checked") or 0),
                                started_at=t0, interval_sec=int(interval) or 1)
            except Exception:  # noqa: BLE001
                pass
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("crypto lifecycle sweep failed")
        if stop_event is not None:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.sleep(interval)


async def crypto_lifecycle_loop() -> None:
    """Named zero-arg factory for workers.base.main / server startup."""
    await run_loop()
