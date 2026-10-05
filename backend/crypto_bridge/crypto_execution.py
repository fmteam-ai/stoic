"""A13 P0-01 — crypto execution truth.

Nothing reaches the exchange before a durable ExecutionIntent exists; the order
carries a deterministic client order id derived from it; the lifecycle is
  created → dispatched → acked|filled|rejected|unknown → reconciled
and after a restart / timeout the exchange is queried BY CLIENT ORDER ID before
anything is ever resent. Every open spot position gets exchange-side OCO
protection, or the position is flattened (fail closed) and ops is alerted.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from bson import ObjectId

from execution_intents import (TERMINAL, create_intent, dedupe_key_for,
                               request_never_left, transition)

logger = logging.getLogger("crypto.execution")

SOURCE = "crypto"
PROTECTION_PLACED = "placed"
PROTECTION_FLATTENED = "flattened_unprotected"
PROTECTION_MISSING = "MISSING"
UNKNOWN_GRACE_SEC = 60


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def client_order_id(intent_id: str) -> str:
    """Deterministic, ≤36 chars, Binance-safe alphabet: 'stoic-' + ULID body."""
    return "stoic-" + intent_id.split("_", 1)[-1].lower()


def dedupe_key(account_id: str, signal: dict) -> str:
    """Same signal/decision → same key (retries converge); anonymous signals never collide."""
    import uuid
    anchor = signal.get("signal_id") or signal.get("scalp_decision_id") or f"anon:{uuid.uuid4().hex}"
    return dedupe_key_for(SOURCE, "open", account_id, anchor)


async def ensure_crypto_indexes(db) -> None:
    await db.trades.create_index("client_order_id", unique=True,
                                 partialFilterExpression={"client_order_id": {"$type": "string"}},
                                 name="uniq_client_order_id")


async def begin(db, *, account_id: str, user_id: str, signal: dict, order_type: str,
                ccxt_symbol: str, side: str, amount: float) -> dict:
    """Durable intent BEFORE any network call. Returns the intent or a denial
    (duplicate in-flight/terminal intent → never a second order)."""
    intent = await create_intent(
        db, source=SOURCE, kind="open", dedupe_key=dedupe_key(account_id, signal),
        account_id=account_id, actor=user_id,
        payload={"symbol": signal.get("symbol"), "exchange_symbol": ccxt_symbol, "side": side,
                 "amount": amount, "order_type": order_type,
                 "stop_loss": signal.get("stop_loss"), "take_profit": signal.get("take_profit"),
                 "signal_id": signal.get("signal_id")})
    if intent.get("duplicate"):
        return {"blocked": "duplicate_intent", "intent_id": intent.get("intent_id"),
                "intent_status": intent.get("status"),
                "in_flight": intent.get("status") not in TERMINAL, "result": intent.get("result")}
    cid = client_order_id(intent["intent_id"])
    await db.execution_intents.update_one({"intent_id": intent["intent_id"]},
                                          {"$set": {"client_order_id": cid}})
    upd = await transition(db, intent["intent_id"], "submitted", detail="crypto engine claimed")
    if upd is not None:
        upd = await transition(db, intent["intent_id"], "dispatched", detail=f"client_order_id {cid}")
    if upd is None:
        return {"blocked": "intent_pipeline_error", "intent_id": intent["intent_id"]}
    return {**upd, "client_order_id": cid}


def outcome_state(order_resp: dict) -> str:
    st = str((order_resp or {}).get("status") or "").lower()
    if st in ("closed", "filled"):
        return "filled"
    if st in ("canceled", "cancelled", "rejected", "expired"):
        return "rejected"
    return "acked"                                   # open / partially filled / pending


def never_left_exchange(exc: BaseException) -> bool:
    """ccxt: an ExchangeError means the venue ANSWERED (safe to reject); network
    failures/timeouts may have executed → UNKNOWN. Else the generic classifier."""
    try:
        import ccxt
        if isinstance(exc, ccxt.NetworkError):
            return False
        if isinstance(exc, ccxt.ExchangeError):
            return True
    except Exception:  # noqa: BLE001
        pass
    return request_never_left(exc)


async def record_failure(db, intent_id: str, exc: BaseException) -> dict:
    """Pre-dispatch / broker-confirmed error → rejected; anything that may have
    left STOIC → UNKNOWN (never resent, reconciled by client order id)."""
    if never_left_exchange(exc):
        await transition(db, intent_id, "rejected", detail=str(exc)[:200],
                         result={"error": str(exc)[:300]})
        return {"blocked": "exchange_error", "intent_id": intent_id, "error": str(exc)[:200]}
    await transition(db, intent_id, "unknown",
                     detail=f"post-dispatch failure ({type(exc).__name__}) — exchange truth required",
                     result={"error": str(exc)[:300]})
    try:
        from alerting import raise_alert
        await raise_alert(db, "crypto_execution_unknown", "critical",
                          f"Crypto order {intent_id} is UNKNOWN after a post-dispatch failure — "
                          "will be reconciled by client order id, never resent.",
                          dedup_key=f"crypto_execution_unknown:{intent_id}", meta={"intent_id": intent_id})
    except Exception:  # noqa: BLE001
        pass
    return {"blocked": "exchange_unknown", "intent_id": intent_id,
            "reason": "exchange outcome unknown — reconciliation pending, order will NOT be resent"}


async def record_outcome(db, intent_id: str, order_resp: dict) -> str:
    state = outcome_state(order_resp)
    await transition(db, intent_id, state,
                     detail=f"exchange order {order_resp.get('id')} status {order_resp.get('status')}",
                     result={"exchange_order_id": str(order_resp.get("id") or ""),
                             "status": order_resp.get("status"),
                             "average": order_resp.get("average"), "filled": order_resp.get("filled")})
    return state


async def protect(db, client, *, trade_id, ccxt_symbol: str, side: str, amount: float,
                  stop_loss: float | None, take_profit: float | None, cid: str, account: dict) -> dict:
    """Exchange-side protection for a filled position; fail closed on any failure."""
    oid = ObjectId(str(trade_id))
    if not (stop_loss and take_profit):
        prot = {"status": PROTECTION_MISSING, "reason": "signal carried no SL/TP", "at": _now()}
    else:
        try:
            placed = await client.place_oco_protection(ccxt_symbol, side, amount, float(stop_loss),
                                                       float(take_profit), cid)
            prot = {"status": PROTECTION_PLACED, "at": _now(), **placed}
        except Exception as e:  # noqa: BLE001
            logger.error("protection failed for %s (%s): %s", trade_id, cid, e)
            prot = {"status": PROTECTION_MISSING, "reason": str(e)[:200], "at": _now()}
    if prot["status"] != PROTECTION_PLACED:
        prot = await _flatten_unprotected(db, client, oid, ccxt_symbol, side, amount, cid, prot)
    await db.trades.update_one({"_id": oid}, {"$set": {"protection": prot,
                                                       **({"status": "closed", "closed_at": _now(),
                                                           "close_reason": "protection_failed_flattened"}
                                                          if prot["status"] == PROTECTION_FLATTENED else {})}})
    return prot


async def _flatten_unprotected(db, client, oid, ccxt_symbol, side, amount, cid, prot) -> dict:
    close_side = "sell" if side.lower() == "buy" else "buy"
    try:
        resp = await client.create_market_order(ccxt_symbol, close_side, amount, client_order_id=f"{cid}-flat")
        prot = {**prot, "status": PROTECTION_FLATTENED, "flatten_order_id": str(resp.get("id") or "")}
        sev, msg = "critical", "Unprotected crypto position FLATTENED (fail closed) — exchange-side SL/TP could not be placed."
    except Exception as e:  # noqa: BLE001
        prot = {**prot, "status": PROTECTION_MISSING, "flatten_error": str(e)[:200]}
        sev, msg = "critical", "Crypto position is OPEN WITHOUT exchange-side protection and could not be flattened — manual action required."
    try:
        from alerting import raise_alert
        await raise_alert(db, "crypto_protection_failed", sev, msg,
                          dedup_key=f"crypto_protection_failed:{oid}",
                          meta={"trade_id": str(oid), "symbol": ccxt_symbol, "protection_status": prot["status"]})
    except Exception:  # noqa: BLE001
        pass
    return prot


def trade_from_exchange_order(intent: dict, order: dict) -> dict:
    """Recovered trade row for an order found at the exchange with no local row."""
    p = intent.get("payload") or {}
    px = float(order.get("average") or order.get("price") or 0)
    st = str(order.get("status") or "").lower()
    return {"user_id": intent.get("actor"), "account_id": intent.get("account_id"),
            "signal_id": p.get("signal_id"), "symbol": p.get("symbol"), "exchange_symbol": p.get("exchange_symbol"),
            "action": "BUY" if str(p.get("side", "")).lower() == "buy" else "SELL",
            "lot_size": float(p.get("amount") or 0), "original_lot_size": float(p.get("amount") or 0),
            "entry_price": round(px, 5), "stop_loss": p.get("stop_loss"), "take_profit": p.get("take_profit"),
            "pnl": 0.0, "exit_price": None,
            "status": "open" if st in ("closed", "filled") else ("pending" if st == "open" else "cancelled"),
            "broker": "BINANCE_SPOT", "broker_kind": "binance", "source": "binance", "mt5_ticket": None,
            "exchange_order_id": str(order.get("id") or ""), "exchange_order_status": order.get("status"),
            "execution_intent_id": intent["intent_id"], "client_order_id": intent.get("client_order_id"),
            "recovered_from_exchange": True, "origin": "auto",
            "opened_at": _now(), "closed_at": None, "error": None,
            "protection": {"status": PROTECTION_MISSING, "reason": "recovered after restart", "at": _now()}}


async def reconcile_intents(db, client_factory, now: datetime | None = None) -> dict:
    """Restart / timeout recovery: every non-terminal crypto intent is resolved by
    querying the exchange BY CLIENT ORDER ID. Found → recorded exactly once
    (trades.client_order_id is unique); not found past the grace → failed_confirmed."""
    now = now or datetime.now(timezone.utc)
    out = {"recorded": 0, "reconciled": 0, "failed_confirmed": 0, "pending": 0, "errors": 0}
    cur = db.execution_intents.find({"source": SOURCE, "status": {"$in": ["dispatched", "unknown", "acked"]}})
    async for it in cur:
        cid = it.get("client_order_id")
        sym = (it.get("payload") or {}).get("exchange_symbol")
        acc = await db.accounts.find_one({"_id": ObjectId(it["account_id"])}) if it.get("account_id") else None
        if not (cid and sym and acc):
            out["errors"] += 1
            continue
        try:
            async with client_factory(acc) as client:
                order = await client.fetch_order_by_client_id(sym, cid)
        except Exception as e:  # noqa: BLE001
            logger.warning("crypto reconcile: exchange query failed for %s: %s", cid, type(e).__name__)
            out["errors"] += 1
            continue
        if order is None:
            age = (now - datetime.fromisoformat(it["created_at"])).total_seconds()
            if age >= UNKNOWN_GRACE_SEC:
                await transition(db, it["intent_id"], "failed_confirmed",
                                 detail="exchange has no order for this client order id")
                out["failed_confirmed"] += 1
            else:
                out["pending"] += 1
            continue
        existing = await db.trades.find_one({"client_order_id": cid}, {"_id": 1})
        if existing is None:
            try:
                await db.trades.insert_one(trade_from_exchange_order(it, order))
                out["recorded"] += 1
            except Exception as e:  # noqa: BLE001 — unique index: a concurrent sweep already recorded it
                logger.info("crypto reconcile: row for %s already present (%s)", cid, type(e).__name__)
        else:
            await db.trades.update_one({"_id": existing["_id"]},
                                       {"$set": {"exchange_order_status": order.get("status")}})
        st = str(order.get("status") or "").lower()
        if st in ("open",):
            if it["status"] != "acked":
                await transition(db, it["intent_id"], "acked", detail="exchange: order open")
            out["pending"] += 1
        else:
            if it["status"] == "unknown":
                await transition(db, it["intent_id"], "reconciled",
                                 detail=f"exchange truth: {st} id {order.get('id')}")
            else:
                await transition(db, it["intent_id"], "filled" if st in ("closed", "filled") else "rejected",
                                 detail=f"exchange truth: {st}")
            out["reconciled"] += 1
    return out


async def unprotected_open_count(db, account_id: str | None = None) -> int:
    q = {"broker_kind": "binance", "status": "open", "protection.status": {"$ne": PROTECTION_PLACED}}
    if account_id:
        q["account_id"] = account_id
    return await db.trades.count_documents(q)


async def reconcile_protection(db, client_factory) -> dict:
    """Every open crypto position must carry exchange-side protection; retry once per
    sweep, otherwise fail closed (flatten) and keep alerting."""
    out = {"protected": 0, "flattened": 0, "missing": 0}
    async for t in db.trades.find({"broker_kind": "binance", "status": "open",
                                   "protection.status": {"$ne": PROTECTION_PLACED}}):
        acc = await db.accounts.find_one({"_id": ObjectId(t["account_id"])})
        if not acc:
            out["missing"] += 1
            continue
        side = "buy" if t.get("action") == "BUY" else "sell"
        try:
            async with client_factory(acc) as client:
                prot = await protect(db, client, trade_id=t["_id"], ccxt_symbol=t.get("exchange_symbol"),
                                     side=side, amount=float(t.get("lot_size") or 0),
                                     stop_loss=t.get("stop_loss"), take_profit=t.get("take_profit"),
                                     cid=t.get("client_order_id") or f"stoic-{str(t['_id'])[-20:]}", account=acc)
        except Exception as e:  # noqa: BLE001
            logger.warning("protection sweep failed for %s: %s", t["_id"], type(e).__name__)
            out["missing"] += 1
            continue
        out["protected" if prot["status"] == PROTECTION_PLACED else
            ("flattened" if prot["status"] == PROTECTION_FLATTENED else "missing")] += 1
    return out


async def reconcile_balances(db, client_factory) -> int:
    """Exchange balance snapshot per live crypto account (equity truth, best effort)."""
    n = 0
    async for acc in db.accounts.find({"kind": "binance", "trading_enabled": True, "status": {"$ne": "deleted"}}):
        try:
            async with client_factory(acc) as client:
                bal = await client.fetch_balance()
            total = float(((bal or {}).get("total") or {}).get("USDT") or 0)
            await db.accounts.update_one({"_id": acc["_id"]},
                                         {"$set": {"exchange_balance_usdt": total, "exchange_balance_at": _now()}})
            n += 1
        except Exception as e:  # noqa: BLE001
            logger.debug("balance reconcile skipped for %s: %s", acc["_id"], type(e).__name__)
    return n


async def reconcile_all(db) -> dict:
    """Worker entry: orders/fills by client id → protection → balances."""
    from crypto_bridge.ccxt_engine import CCXTClient, _live_enabled
    pending = await db.execution_intents.count_documents(
        {"source": SOURCE, "status": {"$in": ["dispatched", "unknown", "acked"]}})
    unprotected = await unprotected_open_count(db)
    if not pending and not unprotected and not _live_enabled():
        return {"skipped": "nothing to reconcile and live crypto is off"}
    res = {"intents": await reconcile_intents(db, CCXTClient),
           "protection": await reconcile_protection(db, CCXTClient)}
    if _live_enabled():
        res["balances"] = await reconcile_balances(db, CCXTClient)
    return res


def stale_cutoff(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
