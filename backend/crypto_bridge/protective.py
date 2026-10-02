"""Exchange-side protective orders for crypto positions (SL / TP / flatten).

Invariant: a filled crypto entry is NEVER left without an exchange-side
stop. `protect_or_flatten` either confirms a resting stop on the exchange
or immediately market-closes the position and closes the trade with
close_reason="protective_order_failed". If even the flatten fails, the
trade stays `open` with protection.status="flatten_failed", a critical ops
alert is raised and the lifecycle sweep (crypto_lifecycle.py) retries on
every cycle.

Strategy per venue (spot unless the ccxt market says `contract`):
  • binance  + TP → native spot OCO (orderList/oco): STOP_LOSS + LIMIT_MAKER
                    (BUY-side OCO uses STOP_LOSS + TAKE_PROFIT). The exchange
                    expires the sibling leg.
  • okx      + TP → native algo OCO (ordType=oco) — one algo order.
  • contracts     → separate reduceOnly STOP_MARKET + reduceOnly TP limit.
  • otherwise     → separate stop-loss order (ccxt `stopLossPrice`) + TP
                    limit. On spot the stop usually locks the balance, so
                    the TP limit may be rejected; that is NOT fatal — the
                    TP becomes a "soft TP" enforced by the lifecycle sweep
                    (cancel stop → market close when price crosses TP).

Client order ids are deterministic per trade (≤32 alnum chars, the OKX
limit): stoic<seed>sl / tp / ol (OCO list) / fx (flatten), seed = the
entry's clientOrderId core (signal id) or the trade ObjectId.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("crypto.protective")

CLOSE_REASON_PROTECTION_FAILED = "protective_order_failed"
# Venue flags needed to fetch/cancel conditional (algo / stop) orders.
_TRIGGER_PARAM_VENUES = {"okx", "kucoin"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _alnum(s) -> str:
    return "".join(ch for ch in str(s or "") if ch.isalnum())


def protection_seed(trade: dict) -> str:
    """Seed for the deterministic protective client ids of `trade`."""
    cid = _alnum(trade.get("client_order_id"))
    if cid.startswith("stoic"):
        cid = cid[5:]
    seed = cid or _alnum(trade.get("_id") or trade.get("id"))
    return seed[:25]


def protective_client_ids(seed: str) -> dict:
    seed = _alnum(seed)[:25]
    return {"sl": f"stoic{seed}sl", "tp": f"stoic{seed}tp",
            "list": f"stoic{seed}ol", "flatten": f"stoic{seed}fx"}


def exit_side(action: str) -> str:
    return "sell" if str(action or "").upper() == "BUY" else "buy"


def exchange_id_of(account: dict) -> str:
    return str((account or {}).get("exchange_id") or "binance").lower()


def protect_amount(trade: dict, entry_order: Optional[dict] = None) -> float:
    """Amount the protective orders must cover: the FILLED entry size minus
    any fee charged in the base asset (Binance spot deducts the buy fee
    from the received coin — a stop for the gross size would be rejected
    for insufficient balance)."""
    o = entry_order or {}
    amt = float(o.get("filled") or 0) or float(trade.get("lot_size") or 0)
    base = str(trade.get("exchange_symbol") or "").split("/")[0].split(":")[0]
    fees = list(o.get("fees") or [])
    if not fees and o.get("fee"):
        fees = [o["fee"]]
    if str(trade.get("action") or "").upper() == "BUY" and base:
        for f in fees:
            try:
                if f and str(f.get("currency") or "").upper() == base.upper():
                    amt -= float(f.get("cost") or 0)
            except (TypeError, ValueError):
                continue
    return max(amt, 0.0)


def order_status(order: Optional[dict]) -> str:
    return str((order or {}).get("status") or "").lower()


def order_fill_price(order: Optional[dict], fallback: float = 0.0) -> float:
    o = order or {}
    for k in ("average", "price"):
        try:
            v = float(o.get(k) or 0)
        except (TypeError, ValueError):
            v = 0.0
        if v > 0:
            return v
    info = o.get("info") or {}
    for k in ("actualPx", "avgPx", "fillPx"):
        try:
            v = float(info.get(k) or 0)
        except (TypeError, ValueError, AttributeError):
            v = 0.0
        if v > 0:
            return v
    return float(fallback or 0)


def quote_fee(order: Optional[dict], quote: str) -> float:
    o = order or {}
    fees = list(o.get("fees") or [])
    if not fees and o.get("fee"):
        fees = [o["fee"]]
    total = 0.0
    for f in fees:
        try:
            if f and str(f.get("currency") or "").upper() == quote.upper():
                total += float(f.get("cost") or 0)
        except (TypeError, ValueError):
            continue
    return total


def compute_pnl(trade: dict, exit_price: float, amount: float,
                exit_order: Optional[dict] = None) -> float:
    direction = 1 if str(trade.get("action") or "").upper() == "BUY" else -1
    entry = float(trade.get("entry_price") or 0)
    quote = (str(trade.get("exchange_symbol") or "").split("/") + [""])[1].split(":")[0]
    gross = direction * (float(exit_price) - entry) * float(amount)
    return round(gross - (quote_fee(exit_order, quote) if quote else 0.0), 8)


async def _audit(db, trade: dict, event: str, detail: dict) -> None:
    try:
        await db.crypto_protection_audit.insert_one({
            "trade_id": str(trade.get("_id") or trade.get("id") or ""),
            "user_id": trade.get("user_id"),
            "account_id": trade.get("account_id"),
            "symbol": trade.get("exchange_symbol") or trade.get("symbol"),
            "event": event, "detail": detail, "at": _now()})
    except Exception as e:  # noqa: BLE001 — audit must not mask the action
        logger.error("crypto protection audit write failed: %s", e)


async def _alert(db, trade: dict, message: str) -> None:
    try:
        from alerting import raise_alert
        await raise_alert(
            db, "crypto_unprotected_position", "critical", message,
            dedup_key=f"crypto_unprotected:{trade.get('_id')}",
            meta={"trade_id": str(trade.get("_id")),
                  "user_id": str(trade.get("user_id")),
                  "symbol": trade.get("exchange_symbol")})
    except Exception as e:  # noqa: BLE001
        logger.error("could not raise crypto unprotected alert: %s", e)


async def place_protection(client, account: dict, trade: dict, amount: float) -> dict:
    """Place SL (+TP). Raises if the STOP could not be confirmed. A TP
    failure is non-fatal (soft TP). Returns the `protection` sub-document."""
    sym = trade["exchange_symbol"]
    side = exit_side(trade.get("action"))
    sl = float(trade.get("stop_loss") or 0)
    tp = float(trade.get("take_profit") or 0)
    if sl <= 0:
        raise ValueError("no stop_loss on trade — cannot protect")
    ids = protective_client_ids(protection_seed(trade))
    exid = exchange_id_of(account)
    await client.load_markets()
    contract = client.is_contract(sym)
    amount = client.amount_to_precision(sym, amount)
    if amount <= 0:
        raise ValueError("protective amount rounds to zero")
    trig = {"trigger": True} if exid in _TRIGGER_PARAM_VENUES else {}
    prot = {"status": "placed", "amount": amount, "side": side,
            "placed_at": _now(), "soft_tp": False, "oco": False,
            "contract": contract, "exchange_id": exid}

    if tp > 0 and not contract and exid == "binance":
        res = await client.create_binance_spot_oco(
            sym, side, amount, sl, tp, list_client_order_id=ids["list"],
            sl_client_order_id=ids["sl"], tp_client_order_id=ids["tp"])
        prot.update({
            "strategy": "binance_oco", "oco": True,
            "list_id": res.get("list_id"), "list_client_order_id": ids["list"],
            "sl": {"order_id": res["sl"]["id"], "client_order_id": ids["sl"],
                   "price": sl, "params": {}},
            "tp": ({"order_id": res["tp"]["id"], "client_order_id": ids["tp"],
                    "price": tp, "params": {}} if res.get("tp") else None)})
        return prot

    if tp > 0 and not contract and exid == "okx":
        o = await client.create_okx_algo_oco(sym, side, amount, sl, tp,
                                             client_order_id=ids["list"])
        leg = {"order_id": str(o.get("id") or ""), "client_order_id": ids["list"],
               "params": dict(trig)}
        prot.update({"strategy": "okx_algo_oco", "oco": True, "single_order": True,
                     "sl": {**leg, "price": sl}, "tp": {**leg, "price": tp}})
        return prot

    # separate orders (stop first — it is the one that must exist)
    o = await client.create_stop_loss_order(sym, side, amount, sl,
                                            client_order_id=ids["sl"],
                                            reduce_only=contract)
    prot.update({"strategy": "separate_reduce_only" if contract else "separate",
                 "sl": {"order_id": str(o.get("id") or ""),
                        "client_order_id": ids["sl"], "price": sl,
                        "params": dict(trig)},
                 "tp": None})
    if tp > 0:
        try:
            t = await client.create_take_profit_limit_order(
                sym, side, amount, tp, client_order_id=ids["tp"],
                reduce_only=contract)
            prot["tp"] = {"order_id": str(t.get("id") or ""),
                          "client_order_id": ids["tp"], "price": tp, "params": {}}
        except Exception as e:  # noqa: BLE001 — stop is in place; TP goes soft
            logger.warning("crypto TP limit rejected (%s) — soft TP via sweep: %s",
                           sym, e)
            prot["soft_tp"] = True
            prot["tp_error"] = f"{type(e).__name__}: {str(e)[:160]}"
    return prot


async def _recover_stop_by_client_id(client, account: dict, trade: dict) -> Optional[dict]:
    """After an SL submission error, the order may still have reached the
    exchange (timeout). Look it up by its deterministic clientOrderId."""
    ids = protective_client_ids(protection_seed(trade))
    exid = exchange_id_of(account)
    trig = {"trigger": True} if exid in _TRIGGER_PARAM_VENUES else {}
    for cid in (ids["sl"], ids["list"]):
        try:
            o = await client.fetch_order_by_client_id(cid, trade["exchange_symbol"], trig)
        except Exception:  # noqa: BLE001 — not found / unsupported
            continue
        if o and order_status(o) == "open":
            return {"order_id": str(o.get("id") or ""), "client_order_id": cid,
                    "price": float(trade.get("stop_loss") or 0), "params": dict(trig)}
    return None


async def protect_or_flatten(db, client, account: dict, trade: dict,
                             amount: Optional[float] = None,
                             entry_order: Optional[dict] = None) -> dict:
    """Ensure `trade` (status open, `_id` set) is protected on the exchange,
    else flatten it. Returns {"outcome": "protected"|"flattened"|
    "flatten_failed", "protection": {...}, ...}. All DB writes are
    status-guarded on status="open"."""
    if amount is None:
        amount = protect_amount(trade, entry_order)
    tid = trade["_id"]
    try:
        prot = await place_protection(client, account, trade, amount)
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {str(e)[:200]}"
        recovered = None
        try:
            recovered = await _recover_stop_by_client_id(client, account, trade)
        except Exception:  # noqa: BLE001
            recovered = None
        if recovered:
            prot = {"status": "placed", "strategy": "separate", "amount": amount,
                    "side": exit_side(trade.get("action")), "placed_at": _now(),
                    "soft_tp": bool(float(trade.get("take_profit") or 0) > 0),
                    "oco": False, "sl": recovered, "tp": None,
                    "recovered_after_error": err,
                    "exchange_id": exchange_id_of(account)}
        else:
            logger.critical("CRYPTO STOP PLACEMENT FAILED trade=%s sym=%s — "
                            "flattening: %s", tid, trade.get("exchange_symbol"), err)
            await _audit(db, trade, "stop_placement_failed", {"error": err})
            return await flatten_and_close(db, client, account, trade, amount,
                                           reason_detail=err)
    sets = {"protection": prot, "confirmed_stop_loss": prot["sl"]["price"],
            "protection_missing": False, "updated_at": _now()}
    if prot["sl"].get("order_id"):
        sets["sl_order_id"] = prot["sl"]["order_id"]
    if prot.get("tp") and prot["tp"].get("order_id"):
        sets["tp_order_id"] = prot["tp"]["order_id"]
    await db.trades.update_one({"_id": tid, "status": "open"}, {"$set": sets})
    await _audit(db, trade, "protection_placed",
                 {"strategy": prot.get("strategy"), "amount": amount,
                  "sl": prot["sl"], "tp": prot.get("tp"),
                  "soft_tp": prot.get("soft_tp")})
    trade["protection"] = prot
    return {"outcome": "protected", "protection": prot}


async def flatten_and_close(db, client, account: dict, trade: dict,
                            amount: float, reason_detail: str = "",
                            close_reason: str = CLOSE_REASON_PROTECTION_FAILED) -> dict:
    """Market-close the position and close the trade doc (status-guarded).
    On flatten failure the trade stays open, flagged flatten_failed, with a
    critical alert — the sweep retries every cycle."""
    tid = trade["_id"]
    sym = trade["exchange_symbol"]
    ids = protective_client_ids(protection_seed(trade))
    contract = False
    try:
        contract = client.is_contract(sym)
    except Exception:  # noqa: BLE001
        contract = False
    # Each retry uses a fresh deterministic clientOrderId (fx, fx01, …).
    attempt = int(((trade.get("protection") or {}).get("flatten_attempts") or 0))

    def _flatten_cid(n: int) -> str:
        return ids["flatten"] if n == 0 else f"{ids['flatten'][:30]}{n % 100:02d}"

    # A previous flatten may have executed even though its response was lost
    # (timeout) — never send a second close for the same position.
    prior = None
    for n in range(max(0, attempt - 3), attempt):
        try:
            cand = await client.fetch_order_by_client_id(_flatten_cid(n), sym)
        except Exception:  # noqa: BLE001 — not found
            continue
        if order_status(cand) == "closed" or float((cand or {}).get("filled") or 0) > 0:
            prior = cand
            break
    amt = client.amount_to_precision(sym, amount)
    try:
        if prior is not None:
            o = prior
        else:
            o = await client.close_position_market(
                sym, exit_side(trade.get("action")), amt,
                client_order_id=_flatten_cid(attempt), reduce_only=contract)
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {str(e)[:200]}"
        logger.critical("CRYPTO FLATTEN FAILED trade=%s sym=%s — position is "
                        "UNPROTECTED: %s", tid, sym, err)
        prot = dict(trade.get("protection") or {})
        prot.update({"status": "flatten_failed", "error": reason_detail[:300],
                     "flatten_error": err, "updated_at": _now(),
                     "flatten_attempts": attempt + 1})
        await db.trades.update_one(
            {"_id": tid, "status": "open"},
            {"$set": {"protection": prot, "protection_missing": False}})
        trade["protection"] = prot
        await _audit(db, trade, "flatten_failed", {"error": err,
                                                    "stop_error": reason_detail})
        await _alert(db, trade, f"Crypto position {sym} trade {tid} has NO stop "
                                f"and the emergency flatten failed: {err}")
        return {"outcome": "flatten_failed", "error": err}
    exit_px = order_fill_price(o, fallback=float(trade.get("entry_price") or 0))
    pnl = compute_pnl(trade, exit_px, amt, o)
    now = _now()
    prot = dict(trade.get("protection") or {})
    prot.update({"status": "flattened", "error": reason_detail[:300],
                 "updated_at": now})
    sets = {"status": "closed", "exit_price": exit_px, "pnl": pnl,
            "closed_at": now, "close_reason": close_reason,
            "exit_order_id": str(o.get("id") or ""),
            "protection": prot}
    r = await db.trades.update_one({"_id": tid, "status": "open"}, {"$set": sets})
    if getattr(r, "modified_count", 1):
        # impr-wiring — free the exposure reservation (idempotent, never raises)
        try:
            from execution_authority import release_reservation
            await release_reservation(db, trade)
        except Exception as e:  # noqa: BLE001 — rebuild heals a miss
            logger.debug("exposure release failed for %s: %s", tid, e)
    await _audit(db, trade, "flattened", {"exit_price": exit_px, "pnl": pnl,
                                          "close_reason": close_reason,
                                          "stop_error": reason_detail})
    trade.update({"status": "closed", "exit_price": exit_px, "pnl": pnl,
                  "closed_at": now, "close_reason": close_reason})
    return {"outcome": "flattened", "exit_price": exit_px, "pnl": pnl,
            "modified": getattr(r, "modified_count", 1)}
