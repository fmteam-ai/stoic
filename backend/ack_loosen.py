"""v1.60.62 #84 — broker-confirmed stop LOOSER than requested: correct once, then alert (optionally close).

The ack keeps being adopted as broker truth (bridge_routes.modification_ack, v1.60.37 flag) — refusing it would only make
our record disagree with the broker while risk, BE and guards assume protection that isn't there. Instead:
1. confirmed SL looser than the REQUESTED SL beyond the broker tolerance (stops_level + 1 point) → ONE fenced corrective
   MODIFY_SL to the requested stop, clamped to the min-stop distance from the live price when known (never a loosen);
2. the correction's own ack still looser beyond tolerance → ops alert WARNING "stop wider than planned" with requested vs
   broker SL and the extra risk in R; no retry loop;
3. BotConfig `ack_loosen_close_enabled` (default OFF): when ON and the extra risk after the correction is > 0.25 R → a
   fenced FULL_CLOSE (mandatory_close, reason ack_loosen_close).
Pure helpers first; the async orchestration (`handle_ack`) is called from the ack route."""
from __future__ import annotations

from datetime import datetime, timezone

CORRECTION_REASON = "ack_loosen_correction"
CLOSE_REASON = "ack_loosen_close"
ALERT_KIND = "stop_wider_than_planned"
CLOSE_EXTRA_R = 0.25
EXTRA_POINTS = 1


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def point_px(account: dict | None, symbol: str) -> float:
    from pip_utils import base_symbol, pip_size
    specs = (account or {}).get("symbol_specs") or {}
    spec = specs.get(base_symbol(symbol)) or specs.get(str(symbol).upper()) or {}
    p = _f(spec.get("point"))
    return p if p and p > 0 else pip_size(symbol) / 10.0


def tolerance_px(account: dict | None, symbol: str) -> float:
    """Broker min-stop (stops_level) + 1 point — the distance the EA may legitimately clamp a stop by."""
    from protection_guard import broker_stop_constraints
    c = broker_stop_constraints(account, symbol) or {}
    return float(c.get("min_stop_distance_px") or 0.0) + EXTRA_POINTS * point_px(account, symbol)


def loosen_px(action: str, requested_sl: float, confirmed_sl: float) -> float:
    """> 0 when the confirmed stop sits on the LOSING side of the requested one (BUY lower, SELL higher)."""
    d = (requested_sl - confirmed_sl) if action == "BUY" else (confirmed_sl - requested_sl)
    return d if d > 0 else 0.0


def extra_risk_r(trade: dict, requested_sl: float, confirmed_sl: float) -> float | None:
    """Extra risk beyond the requested stop, in R of the ORIGINAL planned stop distance."""
    entry = _f(trade.get("entry_price"))
    orig = _f(trade.get("original_stop_loss")) or _f(trade.get("stop_loss"))
    if entry is None or orig is None or abs(entry - orig) <= 0:
        return None
    return round(loosen_px(trade.get("action"), requested_sl, confirmed_sl) / abs(entry - orig), 2)


def corrective_sl(trade: dict, requested_sl: float, confirmed_sl: float, live_price: float | None, min_stop_px: float) -> float | None:
    """The requested stop clamped to the min-stop distance from the live price (when known); only if it still TIGHTENS
    the confirmed stop — never a loosen request."""
    from trade_manager import tightened_sl
    target = float(requested_sl)
    if live_price and live_price > 0 and min_stop_px > 0:
        if trade.get("action") == "BUY":
            target = min(target, live_price - min_stop_px)
        else:
            target = max(target, live_price + min_stop_px)
    return tightened_sl({**trade, "stop_loss": confirmed_sl}, round(target, 5))


def decide(trade: dict, account: dict | None, cfg: dict | None, *, requested_sl, confirmed_sl, pending_reason: str | None,
           live_price: float | None = None) -> dict:
    """Pure decision for a SUCCESSFUL MODIFY_SL ack.
    → {"action": "none" | "correct" | "alert" | "close", ...detail}"""
    req, con = _f(requested_sl), _f(confirmed_sl)
    if req is None or con is None or trade.get("action") not in ("BUY", "SELL"):
        return {"action": "none", "why": "no requested/confirmed stop"}
    symbol = trade.get("symbol") or ""
    tol = tolerance_px(account, symbol)
    loosen = loosen_px(trade["action"], req, con)
    base = {"requested_sl": req, "confirmed_sl": con, "loosen_px": round(loosen, 5), "tolerance_px": round(tol, 5)}
    if loosen <= tol:
        return {"action": "none", "why": "within broker tolerance" if loosen > 0 else "not loosened", **base}
    extra_r = extra_risk_r(trade, req, con)
    if pending_reason == CORRECTION_REASON or (trade.get("ack_loosen_correction") or {}).get("intent_id"):
        close_on = bool((cfg or {}).get("ack_loosen_close_enabled"))
        if close_on and extra_r is not None and extra_r > CLOSE_EXTRA_R:
            return {"action": "close", "extra_risk_r": extra_r, **base}
        return {"action": "alert", "extra_risk_r": extra_r, **base}
    from protection_guard import broker_stop_constraints
    min_stop = float((broker_stop_constraints(account, symbol) or {}).get("min_stop_distance_px") or 0.0)
    new_sl = corrective_sl(trade, req, con, live_price, min_stop)
    if new_sl is None:
        return {"action": "alert", "extra_risk_r": extra_r, "why": "correction would not tighten", **base}
    return {"action": "correct", "new_sl": new_sl, "extra_risk_r": extra_r, **base}


async def handle_ack(db, trade: dict, account: dict, *, requested_sl, confirmed_sl, pending_reason: str | None) -> dict:
    """Route hook (after the ack has been adopted). Returns the decision with what was done."""
    from bson import ObjectId
    cfg = None
    try:
        cfg = await db.bot_configs.find_one({"user_id": trade.get("user_id"), "account_id": str(account["_id"])}) \
            or await db.bot_configs.find_one({"user_id": trade.get("user_id")})
    except Exception:  # noqa: BLE001
        cfg = None
    live = _f(trade.get("live_price")) or _f(trade.get("current_price"))
    d = decide(trade, account, cfg or {}, requested_sl=requested_sl, confirmed_sl=confirmed_sl,
               pending_reason=pending_reason, live_price=live)
    now = datetime.now(timezone.utc).isoformat()
    tid = trade["_id"] if not isinstance(trade["_id"], str) else ObjectId(trade["_id"])
    if d["action"] == "correct":
        from command_fence import stamp_pending_modification
        stamped = await stamp_pending_modification(
            db, {"_id": tid, "status": "open", "pending_modification": None},
            {"type": "MODIFY_SL", "new_sl": float(d["new_sl"]), "reason": CORRECTION_REASON},
            extra_set={"ack_loosen_correction": {"requested_sl": d["requested_sl"], "confirmed_sl": d["confirmed_sl"],
                                                 "new_sl": float(d["new_sl"]), "at": now, "intent_id": "pending"}})
        if stamped:
            await db.trades.update_one({"_id": tid}, {"$set": {"ack_loosen_correction.intent_id": stamped.get("intent_id")}})
        d["dispatched"] = bool(stamped)
        return d
    if d["action"] in ("alert", "close"):
        from alerting import raise_alert
        msg = (f"stop wider than planned — {trade.get('symbol')} {trade.get('action')} trade {trade['_id']}: requested SL "
               f"{d['requested_sl']} / broker SL {d['confirmed_sl']} (+{d['loosen_px']} beyond, tolerance {d['tolerance_px']}), "
               f"extra risk {d.get('extra_risk_r') if d.get('extra_risk_r') is not None else '?'} R")
        await raise_alert(db, ALERT_KIND, "warning", msg, dedup_key=f"{ALERT_KIND}:{trade['_id']}",
                          meta={"trade_id": str(trade["_id"]), "account_id": str(account["_id"]), **{k: v for k, v in d.items() if k != "action"}})
        await db.trades.update_one({"_id": tid}, {"$set": {"ack_loosen_correction.outcome": d["action"], "ack_loosen_correction.alerted_at": now}})
        if d["action"] == "close":
            from close_commands import mandatory_close
            d["close_result"] = await mandatory_close(db, {**trade, "_id": tid}, reason=CLOSE_REASON, actor="ack_loosen",
                                                      stamp={"close_reason_pending": CLOSE_REASON}, account=account)
    return d
