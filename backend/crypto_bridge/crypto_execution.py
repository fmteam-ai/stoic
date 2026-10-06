"""A13 P0-01 — crypto execution truth (main97 N97-7/8/9 hardening).

Nothing reaches the exchange before a durable ExecutionIntent exists; the order
carries a deterministic client order id derived from it; the lifecycle is
  created → submitted → dispatched → acked|filled|rejected|unknown → reconciled
and after a restart / timeout the exchange is queried BY CLIENT ORDER ID before
anything is ever resent. Every filled spot BUY gets exchange-side OCO protection
sized from the FILLED amount minus base-asset fees, or the position is flattened
(fail closed) and ops is alerted. Spot SELLs reduce exposure — nothing to protect.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from execution_intents import (TERMINAL, create_intent, dedupe_key_for,
                               request_never_left, transition)

logger = logging.getLogger("crypto.execution")

SOURCE = "crypto"
PROTECTION_PLACED = "placed"
PROTECTION_PLACING = "placing"
PROTECTION_NA = "not_applicable"
PROTECTION_FLATTENED = "flattened_unprotected"
PROTECTION_MISSING = "MISSING"
PROTECTED_STATES = (PROTECTION_PLACED, PROTECTION_NA)
UNKNOWN_GRACE_SEC = 60
PLACING_LEASE_SEC = 120
MAX_CLIENT_ID_LEN = 36          # Binance: ^[\.A-Z\:/a-z0-9_-]{1,36}$


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def client_order_id(intent_id: str) -> str:
    """Deterministic, Binance-safe: 'stoic-' + 26-char ULID body = 32 chars."""
    cid = "stoic-" + intent_id.split("_", 1)[-1].lower()
    assert len(cid) <= MAX_CLIENT_ID_LEN - 4, cid          # room for the longest suffix
    return cid


def derived_id(cid: str, suffix: str) -> str:
    """N97-7 — every derived id (flatten / OCO legs) must stay within the exchange limit."""
    out = f"{cid}{suffix}"
    if len(out) > MAX_CLIENT_ID_LEN:
        raise ValueError(f"client order id too long ({len(out)} > {MAX_CLIENT_ID_LEN}): {out}")
    return out


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
                ccxt_symbol: str, side: str, amount: float, reservation_id: str | None = None) -> dict:
    """Durable intent BEFORE any network call. Returns the intent or a denial
    (duplicate in-flight/terminal intent → never a second order)."""
    intent = await create_intent(
        db, source=SOURCE, kind="open", dedupe_key=dedupe_key(account_id, signal),
        account_id=account_id, actor=user_id,
        payload={"symbol": signal.get("symbol"), "exchange_symbol": ccxt_symbol, "side": side,
                 "amount": amount, "order_type": order_type,
                 "stop_loss": signal.get("stop_loss"), "take_profit": signal.get("take_profit"),
                 "signal_id": signal.get("signal_id"), "origin": signal.get("origin", "manual"),
                 "reservation_id": reservation_id})
    if intent.get("duplicate"):
        return {"blocked": "duplicate_intent", "intent_id": intent.get("intent_id"),
                "intent_status": intent.get("status"),
                "in_flight": intent.get("status") not in TERMINAL, "result": intent.get("result")}
    cid = client_order_id(intent["intent_id"])
    await db.execution_intents.update_one({"intent_id": intent["intent_id"]},
                                          {"$set": {"client_order_id": cid, "trade_recorded": False}})
    upd = await transition(db, intent["intent_id"], "submitted", detail="crypto engine claimed")
    if upd is not None:
        upd = await transition(db, intent["intent_id"], "dispatched", detail=f"client_order_id {cid}")
    if upd is None:
        return {"blocked": "intent_pipeline_error", "intent_id": intent["intent_id"]}
    return {**upd, "client_order_id": cid}


def outcome_state(order_resp: dict) -> str:
    st = str((order_resp or {}).get("status") or "").lower()
    filled = float((order_resp or {}).get("filled") or 0)
    if st in ("closed", "filled"):
        return "filled"
    if st in ("canceled", "cancelled", "rejected", "expired"):
        return "filled" if filled > 0 else "rejected"   # N98-10 — a partly filled order IS a position
    return "acked"                                   # open / partially filled / pending


def filled_base_amount(order: dict, base_asset: str) -> float:
    """N97-8 — what we actually HOLD: filled quantity minus fees charged in the base asset."""
    filled = float((order or {}).get("filled") or 0)
    fees = list((order or {}).get("fees") or [])
    if not fees and (order or {}).get("fee"):
        fees = [order["fee"]]
    base_fee = sum(float(f.get("cost") or 0) for f in fees
                   if str(f.get("currency") or "").upper() == base_asset.upper())
    return max(0.0, filled - base_fee)


SETUP_ERRORS = (ValueError, TypeError, KeyError, AttributeError, RuntimeError)


def never_left_exchange(exc: BaseException) -> bool:
    """ccxt: an ExchangeError means the venue ANSWERED (safe to reject); network
    failures/timeouts may have executed → UNKNOWN. Client-setup faults (bad symbol,
    SandboxUnavailable, decryption, precision) never reached the venue → rejected."""
    try:
        from crypto_bridge.ccxt_engine import SandboxUnavailable
        if isinstance(exc, SandboxUnavailable):
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        import ccxt
        if isinstance(exc, ccxt.NetworkError):
            return False
        if isinstance(exc, ccxt.ExchangeError):
            return True
    except Exception:  # noqa: BLE001
        pass
    if isinstance(exc, SETUP_ERRORS) and not isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    return request_never_left(exc)


async def _alert(db, kind: str, severity: str, msg: str, dedup: str, meta: dict) -> None:
    try:
        from alerting import raise_alert
        await raise_alert(db, kind, severity, msg, dedup_key=dedup, meta=meta)
    except Exception:  # noqa: BLE001
        pass


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
    await _alert(db, "crypto_execution_unknown", "critical",
                 f"Crypto order {intent_id} is UNKNOWN after a post-dispatch failure — "
                 "will be reconciled by client order id, never resent.",
                 f"crypto_execution_unknown:{intent_id}", {"intent_id": intent_id})
    return {"blocked": "exchange_unknown", "intent_id": intent_id,
            "reason": "exchange outcome unknown — reconciliation pending, order will NOT be resent"}


async def record_outcome(db, intent_id: str, order_resp: dict) -> str:
    state = outcome_state(order_resp)
    await transition(db, intent_id, state,
                     detail=f"exchange order {order_resp.get('id')} status {order_resp.get('status')}",
                     result={"exchange_order_id": str(order_resp.get("id") or ""),
                             "status": order_resp.get("status"),
                             "average": order_resp.get("average"), "filled": order_resp.get("filled"),
                             "fees": order_resp.get("fees") or ([order_resp["fee"]] if order_resp.get("fee") else [])})
    return state


async def mark_trade_recorded(db, intent_id: str, trade_id) -> None:
    await db.execution_intents.update_one({"intent_id": intent_id},
                                          {"$set": {"trade_recorded": True, "trade_id": str(trade_id)}})


async def insert_trade_once(db, trade_doc: dict):
    """N97-9 — the request path and the recovery sweep may race on the same client order id:
    the unique index decides, the loser adopts the winner's row."""
    try:
        r = await db.trades.insert_one(trade_doc)
        return r.inserted_id, True
    except DuplicateKeyError:
        existing = await db.trades.find_one({"client_order_id": trade_doc["client_order_id"]}, {"_id": 1})
        return existing["_id"], False


# ── protection ───────────────────────────────────────────────────────────────
async def _claim_protection(db, oid) -> bool:
    """N97-9 — one actor places protection at a time (request path vs sweep)."""
    lease_floor = (datetime.now(timezone.utc) - timedelta(seconds=PLACING_LEASE_SEC)).isoformat()
    res = await db.trades.update_one(
        {"_id": oid, "status": "open",
         "$or": [{"protection.status": {"$nin": [PROTECTION_PLACED, PROTECTION_NA, PROTECTION_PLACING]}},
                 {"protection.status": PROTECTION_PLACING, "protection.at": {"$lt": lease_floor}},
                 {"protection.status": PROTECTION_PLACING, "protection.uncertain": True}]},
        {"$set": {"protection.status": PROTECTION_PLACING, "protection.at": _now()}})
    return res.matched_count == 1


async def protect(db, client, *, trade_id, ccxt_symbol: str, side: str, amount: float,
                  stop_loss: float | None, take_profit: float | None, cid: str, account: dict) -> dict:
    """Exchange-side protection for a filled position (amount = HELD base amount);
    fail closed on any failure. Spot SELL: nothing to protect."""
    oid = ObjectId(str(trade_id))
    if side.lower() != "buy":
        prot = {"status": PROTECTION_NA, "reason": "spot SELL reduces exposure — no position to protect", "at": _now()}
        await db.trades.update_one({"_id": oid}, {"$set": {"protection": prot}})
        return prot
    if not await _claim_protection(db, oid):
        cur = await db.trades.find_one({"_id": oid}, {"protection": 1})
        return (cur or {}).get("protection") or {"status": PROTECTION_PLACING}
    # N98-9 — never resend blind: an earlier attempt (timeout / expired lease) may have placed the list
    try:
        live = await client.fetch_oco_status(derived_id(cid, "-oco"))
    except Exception as e:  # noqa: BLE001 — transport fault: UNCERTAIN, keep the lease, try next sweep
        prot = {"status": PROTECTION_PLACING, "at": _now(), "uncertain": True,
                "reason": f"OCO lookup failed ({type(e).__name__}) — not resent"}
        await db.trades.update_one({"_id": oid}, {"$set": {"protection": prot}})
        return prot
    if live is not None:
        prot = {"status": PROTECTION_PLACED, "at": _now(), "amount": amount, "list_id": str(live.get("orderListId") or ""),
                "list_client_order_id": derived_id(cid, "-oco"), "tp_client_order_id": derived_id(cid, "-tp"),
                "sl_client_order_id": derived_id(cid, "-sl"), "raw_status": live.get("listOrderStatus"), "adopted": True}
        await db.trades.update_one({"_id": oid}, {"$set": {"protection": prot}})
        return prot
    if amount <= 0:
        prot = {"status": PROTECTION_MISSING, "reason": "no held base amount after fees", "at": _now()}
    elif not (stop_loss and take_profit):
        prot = {"status": PROTECTION_MISSING, "reason": "signal carried no SL/TP", "at": _now()}
    else:
        try:
            qty = client.amount_to_precision(ccxt_symbol, amount)
            placed = await client.place_oco_protection(ccxt_symbol, side, qty, float(stop_loss),
                                                       float(take_profit), cid)
            prot = {"status": PROTECTION_PLACED, "at": _now(), "amount": qty, **placed}
        except Exception as e:  # noqa: BLE001
            logger.error("protection failed for %s (%s): %s", trade_id, cid, e)
            if not never_left_exchange(e):
                # N98-9 — a TIMEOUT may have placed the OCO: uncertain, never flatten over a live list
                prot = {"status": PROTECTION_PLACING, "at": _now(), "uncertain": True,
                        "reason": f"OCO outcome unknown ({type(e).__name__}) — verified by client id next sweep"}
                await db.trades.update_one({"_id": oid}, {"$set": {"protection": prot}})
                await _alert(db, "crypto_protection_uncertain", "warning",
                             "Crypto OCO outcome unknown after a transport failure — will be verified by client id.",
                             f"crypto_protection_uncertain:{oid}", {"trade_id": str(oid), "symbol": ccxt_symbol})
                return prot
            prot = {"status": PROTECTION_MISSING, "reason": str(e)[:200], "at": _now()}
    if prot["status"] != PROTECTION_PLACED:
        prot = await _flatten_unprotected(db, client, oid, ccxt_symbol, side, amount, cid, prot)
    sets = {"protection": prot}
    if prot["status"] == PROTECTION_FLATTENED:
        sets.update({"status": "closed", "closed_at": _now(), "close_reason": "protection_failed_flattened",
                     "exit_price": prot.get("flatten_price")})
    await db.trades.update_one({"_id": oid}, {"$set": sets})
    return prot


async def _flatten_unprotected(db, client, oid, ccxt_symbol, side, amount, cid, prot) -> dict:
    close_side = "sell" if side.lower() == "buy" else "buy"
    try:
        prior = await client.fetch_order_by_client_id(ccxt_symbol, derived_id(cid, "-fl"))   # N98-9 — never resend
        if prior is not None:
            return {**prot, "status": PROTECTION_FLATTENED, "flatten_order_id": str(prior.get("id") or ""),
                    "flatten_price": prior.get("average") or prior.get("price"), "flatten_amount": prior.get("filled"),
                    "adopted": True}
        qty = client.amount_to_precision(ccxt_symbol, amount)
        if qty <= 0:
            raise ValueError("nothing to flatten")
        resp = await client.create_market_order(ccxt_symbol, close_side, qty, client_order_id=derived_id(cid, "-fl"))
        prot = {**prot, "status": PROTECTION_FLATTENED, "flatten_order_id": str(resp.get("id") or ""),
                "flatten_price": resp.get("average") or resp.get("price"), "flatten_amount": qty}
        msg = "Unprotected crypto position FLATTENED (fail closed) — exchange-side SL/TP could not be placed."
    except Exception as e:  # noqa: BLE001
        prot = {**prot, "status": PROTECTION_MISSING, "flatten_error": str(e)[:200]}
        msg = "Crypto position is OPEN WITHOUT exchange-side protection and could not be flattened — manual action required."
    await _alert(db, "crypto_protection_failed", "critical", msg, f"crypto_protection_failed:{oid}",
                 {"trade_id": str(oid), "symbol": ccxt_symbol, "protection_status": prot["status"]})
    return prot


# ── recovery ─────────────────────────────────────────────────────────────────
def trade_from_exchange_order(intent: dict, order: dict) -> dict:
    """Recovered trade row for an order found at the exchange with no local row."""
    p = intent.get("payload") or {}
    px = float(order.get("average") or order.get("price") or 0)
    st = str(order.get("status") or "").lower()
    base = str(p.get("exchange_symbol") or "/").split("/")[0]
    partial = st in ("canceled", "cancelled", "expired") and float(order.get("filled") or 0) > 0
    held = filled_base_amount(order, base) if (st in ("closed", "filled") or partial) else 0.0
    return {"user_id": intent.get("actor"), "account_id": intent.get("account_id"),
            "signal_id": p.get("signal_id"), "symbol": p.get("symbol"), "exchange_symbol": p.get("exchange_symbol"),
            "action": "BUY" if str(p.get("side", "")).lower() == "buy" else "SELL",
            "lot_size": float(p.get("amount") or 0), "original_lot_size": float(p.get("amount") or 0),
            "held_amount": held,
            "entry_price": round(px, 5), "stop_loss": p.get("stop_loss"), "take_profit": p.get("take_profit"),
            "pnl": 0.0, "exit_price": None,
            "status": "open" if (st in ("closed", "filled") or partial) else ("pending" if st == "open" else "cancelled"),
            "broker": "BINANCE_SPOT", "broker_kind": "binance", "source": "binance", "mt5_ticket": None,
            "exchange_order_id": str(order.get("id") or ""), "exchange_order_status": order.get("status"),
            "execution_intent_id": intent["intent_id"], "client_order_id": intent.get("client_order_id"),
            "reservation_id": p.get("reservation_id"),
            "recovered_from_exchange": True, "origin": p.get("origin") or "auto",
            "opened_at": _now(), "closed_at": None, "error": None,
            "protection": {"status": PROTECTION_MISSING, "reason": "recovered after restart", "at": _now()}}


async def _settle_reservation(db, intent: dict, trade_id=None, released_reason: str | None = None) -> None:
    """N97-9 — an UNKNOWN outcome kept its reservation held; broker truth settles it."""
    rid = (intent.get("payload") or {}).get("reservation_id")
    if not rid:
        return
    import account_reservations as ar
    if trade_id is not None:
        await db.risk_reservations.update_one({"reservation_id": rid}, {"$set": {"uncertain": False}})
        await ar.link_trade(db, rid, str(trade_id))
    else:
        await ar.release(db, rid, released_reason or "exchange_not_found")


async def _fail_confirmed(db, intent: dict) -> None:
    """dispatched/acked → unknown → failed_confirmed (the table forbids the direct jump)."""
    if intent["status"] in ("dispatched", "acked"):
        await transition(db, intent["intent_id"], "unknown", detail="exchange has no order for this client order id")
    await transition(db, intent["intent_id"], "failed_confirmed",
                     detail="exchange has no order for this client order id")
    await _settle_reservation(db, intent, released_reason="exchange_not_found")


async def _apply_exchange_truth(db, intent: dict, order: dict) -> tuple[str, bool]:
    """Record / update the local row from exchange truth. Returns (status, recorded_new_row)."""
    st = str(order.get("status") or "").lower()
    base = str((intent.get("payload") or {}).get("exchange_symbol") or "/").split("/")[0]
    existing = await db.trades.find_one({"client_order_id": intent["client_order_id"]}, {"_id": 1, "status": 1})
    recorded = False
    if existing is None:
        tid, recorded = await insert_trade_once(db, trade_from_exchange_order(intent, order))
    else:
        tid = existing["_id"]
        sets = {"exchange_order_status": order.get("status")}
        if st in ("closed", "filled") and existing.get("status") == "pending":
            # N97-9 — a filled LIMIT order becomes an OPEN position (and gets protected by the sweep)
            sets.update({"status": "open", "entry_price": round(float(order.get("average") or order.get("price") or 0), 5),
                         "held_amount": filled_base_amount(order, base), "filled_at": _now()})
        elif st in ("canceled", "cancelled", "rejected", "expired") and existing.get("status") == "pending":
            if float(order.get("filled") or 0) > 0:          # N98-10 — partial fill before cancel = open position
                sets.update({"status": "open", "entry_price": round(float(order.get("average") or order.get("price") or 0), 5),
                             "held_amount": filled_base_amount(order, base), "filled_at": _now(), "partial_fill": True})
            else:
                sets.update({"status": "cancelled", "closed_at": _now(), "close_reason": f"exchange_{st}"})
        await db.trades.update_one({"_id": tid}, {"$set": sets})
    await mark_trade_recorded(db, intent["intent_id"], tid)
    if st not in ("open",):
        is_position = st in ("closed", "filled") or float(order.get("filled") or 0) > 0
        await _settle_reservation(db, intent, trade_id=tid) if is_position \
            else await _settle_reservation(db, intent, released_reason=f"exchange_{st}")
    return st, recorded


async def reconcile_intents(db, client_factory, now: datetime | None = None) -> dict:
    """Restart / timeout recovery: every non-terminal crypto intent — and every FILLED
    intent whose trade row was never written (crash window) — is resolved by querying
    the exchange BY CLIENT ORDER ID. Found → recorded exactly once; not found past the
    grace → failed_confirmed."""
    now = now or datetime.now(timezone.utc)
    out = {"recorded": 0, "reconciled": 0, "failed_confirmed": 0, "pending": 0, "errors": 0}
    q = {"source": SOURCE, "$or": [{"status": {"$in": ["dispatched", "unknown", "acked"]}},
                                   {"status": "filled", "trade_recorded": {"$ne": True}}]}
    async for it in db.execution_intents.find(q):
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
            if age >= UNKNOWN_GRACE_SEC and it["status"] != "filled":
                await _fail_confirmed(db, it)
                out["failed_confirmed"] += 1
            else:
                out["pending"] += 1
            continue
        st, recorded = await _apply_exchange_truth(db, it, order)
        out["recorded"] += int(recorded)
        if st == "open":
            if it["status"] in ("dispatched", "unknown"):
                await transition(db, it["intent_id"], "acked", detail="exchange: order open")
            out["pending"] += 1
            continue
        if it["status"] == "unknown":
            await transition(db, it["intent_id"], "reconciled", detail=f"exchange truth: {st} id {order.get('id')}")
        elif it["status"] in ("dispatched", "acked"):
            await transition(db, it["intent_id"], "filled" if st in ("closed", "filled") else "rejected",
                             detail=f"exchange truth: {st}")
        out["reconciled"] += 1
    return out


async def unprotected_open_count(db, account_id: str | None = None) -> int:
    q = {"broker_kind": "binance", "status": "open", "protection.status": {"$nin": list(PROTECTED_STATES)}}
    if account_id:
        q["account_id"] = account_id
    return await db.trades.count_documents(q)


async def reconcile_protection(db, client_factory) -> dict:
    """Every open crypto BUY must carry exchange-side protection; retry once per sweep
    (skipping rows another actor is placing), otherwise fail closed (flatten) and keep alerting."""
    out = {"protected": 0, "flattened": 0, "missing": 0, "skipped": 0}
    lease_floor = (datetime.now(timezone.utc) - timedelta(seconds=PLACING_LEASE_SEC)).isoformat()
    async for t in db.trades.find({"broker_kind": "binance", "status": "open",
                                   "protection.status": {"$nin": list(PROTECTED_STATES)}}):
        prot = t.get("protection") or {}
        if prot.get("status") == PROTECTION_PLACING and str(prot.get("at") or "") >= lease_floor \
                and not prot.get("uncertain"):
            out["skipped"] += 1                      # the request path is on it
            continue
        acc = await db.accounts.find_one({"_id": ObjectId(t["account_id"])})
        if not acc:
            out["missing"] += 1
            continue
        side = "buy" if t.get("action") == "BUY" else "sell"
        amount = float(t.get("held_amount") if t.get("held_amount") is not None else (t.get("lot_size") or 0))
        try:
            async with client_factory(acc) as client:
                res = await protect(db, client, trade_id=t["_id"], ccxt_symbol=t.get("exchange_symbol"),
                                    side=side, amount=amount, stop_loss=t.get("stop_loss"),
                                    take_profit=t.get("take_profit"),
                                    cid=t.get("client_order_id") or f"stoic-{str(t['_id'])[-20:]}", account=acc)
        except Exception as e:  # noqa: BLE001
            logger.warning("protection sweep failed for %s: %s", t["_id"], type(e).__name__)
            out["missing"] += 1
            continue
        key = {PROTECTION_PLACED: "protected", PROTECTION_NA: "protected",
               PROTECTION_FLATTENED: "flattened"}.get(res["status"], "missing")
        out[key] += 1
    return out


async def reconcile_protected_positions(db, client_factory) -> int:
    """N97-9 — a filled OCO leg closes the local trade with the leg's fill price."""
    closed = 0
    async for t in db.trades.find({"broker_kind": "binance", "status": "open", "protection.status": PROTECTION_PLACED,
                                   "protection.list_client_order_id": {"$type": "string"}}):
        acc = await db.accounts.find_one({"_id": ObjectId(t["account_id"])})
        if not acc:
            continue
        prot = t["protection"]
        try:
            async with client_factory(acc) as client:
                lst = await client.fetch_oco_status(prot["list_client_order_id"])
                if not lst or str(lst.get("listOrderStatus") or "").upper() != "ALL_DONE":
                    continue
                leg = None
                for leg_id, reason in ((prot.get("tp_client_order_id"), "tp"), (prot.get("sl_client_order_id"), "sl")):
                    if not leg_id:
                        continue
                    o = await client.fetch_order_by_client_id(t.get("exchange_symbol"), leg_id)
                    if o and str(o.get("status") or "").lower() in ("closed", "filled"):
                        leg = (o, reason)
                        break
        except Exception as e:  # noqa: BLE001
            logger.warning("protected-position sweep failed for %s: %s", t["_id"], type(e).__name__)
            continue
        if leg is None:
            # N98-10 — list finished with NO fill (cancelled/expired): the position is unprotected again
            await db.trades.update_one({"_id": t["_id"]}, {"$set": {"protection": {
                "status": PROTECTION_MISSING, "reason": "OCO list ended without a fill", "at": _now()}}})
            continue
        o, reason = leg
        exit_px = float(o.get("average") or o.get("price") or 0)
        qty = float(o.get("filled") or t.get("held_amount") or t.get("lot_size") or 0)
        pnl = round((exit_px - float(t.get("entry_price") or 0)) * qty, 4)
        await db.trades.update_one({"_id": t["_id"]}, {"$set": {
            "status": "closed", "closed_at": _now(), "exit_price": exit_px, "pnl": pnl,
            "close_reason": f"oco_{reason}", "protection.filled_leg": reason}})
        if t.get("reservation_id"):
            from scalp.risk_reservations import release_for_trade
            await release_for_trade(db, str(t["_id"]), f"oco_{reason}")
        closed += 1
    return closed


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
    """Worker entry: orders/fills by client id → protection → OCO fills → balances."""
    from crypto_bridge.ccxt_engine import CCXTClient, _live_enabled
    pending = await db.execution_intents.count_documents(
        {"source": SOURCE, "$or": [{"status": {"$in": ["dispatched", "unknown", "acked"]}},
                                   {"status": "filled", "trade_recorded": {"$ne": True}}]})
    open_crypto = await db.trades.count_documents({"broker_kind": "binance", "status": "open"})
    if not pending and not open_crypto and not _live_enabled():
        return {"skipped": "nothing to reconcile and live crypto is off"}
    res = {"intents": await reconcile_intents(db, CCXTClient),
           "protection": await reconcile_protection(db, CCXTClient),
           "oco_closed": await reconcile_protected_positions(db, CCXTClient)}
    if _live_enabled():
        res["balances"] = await reconcile_balances(db, CCXTClient)
    return res
