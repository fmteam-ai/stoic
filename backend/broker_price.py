"""v1.60.53 (D) — broker price for LIVE trade management. The EA reports each open position's current price every
heartbeat (/bridge/report → trade.live_price + live_at). Tier management (BE / TP1–TP3 / de-risk / close) must use THAT
price, never a web quote (Yahoo/Stooq differ from the broker and failed ×4 at 20:01 on 15 Oct)."""
from __future__ import annotations

import os
from datetime import datetime, timezone

FRESH_MS = int(os.environ.get("BROKER_PRICE_FRESH_MS", "15000"))        # fresh = age ≤ 15 s (configurable)
HEALTH_AMBER_MS = int(os.environ.get("BROKER_PRICE_AMBER_MS", "60000"))  # Bot Health amber when any open trade > 60 s


def _epoch_ms(ts) -> float | None:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.timestamp() * 1000.0
    if isinstance(ts, (int, float)):
        return float(ts) * (1000.0 if ts < 1e12 else 1.0)
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp() * 1000.0
    except ValueError:
        return None


def ea_at_least(ea_version, major: int, minor: int) -> bool:
    try:
        parts = str(ea_version or "0").split(".")
        return (int(parts[0]), int(parts[1]) if len(parts) > 1 else 0) >= (major, minor)
    except (TypeError, ValueError):
        return False


def from_report(trade: dict, now_ms: float | None = None, ea_version=None) -> dict | None:
    """Broker snapshot carried on the trade doc (live_price / live_bid / live_ask + live_price_at).
    #47 — `live_at` is refreshed by EVERY heartbeat (R-6 volume), also by EAs that report no current_price: a
    live_price paired only with legacy `live_at` is treated as fresh ONLY when the EA is ≥ 1.66 (reports a price on
    every heartbeat); otherwise `live_price_at` (v1.60.53 receive stamp) is required."""
    price = trade.get("live_price")
    at = _epoch_ms(trade.get("live_price_at"))
    if at is None and ea_at_least(ea_version, 1, 66):
        at = _epoch_ms(trade.get("live_at"))
    if price is None or at is None:
        return None
    now_ms = now_ms if now_ms is not None else datetime.now(timezone.utc).timestamp() * 1000.0
    return {"price": float(price), "bid": trade.get("live_bid"), "ask": trade.get("live_ask"),
            "age_ms": max(0, int(now_ms - at)), "source": "broker"}


def from_scalp_tick(tick: dict | None, now_ms: float | None = None) -> dict | None:
    """Fallback: the scalp tick stream for the same account+symbol ({bid, ask, tm_ms|ts_ms})."""
    if not tick:
        return None
    bid, ask = tick.get("bid"), tick.get("ask")
    at = tick.get("ts_ms") or tick.get("tm_ms") or _epoch_ms(tick.get("ts"))
    if bid is None or ask is None or at is None:
        return None
    now_ms = now_ms if now_ms is not None else datetime.now(timezone.utc).timestamp() * 1000.0
    return {"price": (float(bid) + float(ask)) / 2.0, "bid": float(bid), "ask": float(ask),
            "age_ms": max(0, int(now_ms - float(at))), "source": "scalp_tick"}


def management_price(snap: dict, action: str) -> tuple[float, str]:
    """Direction-correct: a BUY is managed/closed on BID, a SELL on ASK; mid/current when only one price exists."""
    side = (action or "").upper()
    if side == "BUY" and snap.get("bid") is not None:
        return float(snap["bid"]), "bid"
    if side == "SELL" and snap.get("ask") is not None:
        return float(snap["ask"]), "ask"
    return float(snap["price"]), "current_price"


def for_trade(trade: dict, scalp_tick: dict | None = None, now_ms: float | None = None,
              fresh_ms: int | None = None, ea_version=None) -> dict:
    """{price, bid, ask, age_ms, source, fresh, used, note}. Never falls back to web quotes: stale/missing →
    fresh=False and the caller skips this management cycle."""
    fresh_ms = FRESH_MS if fresh_ms is None else fresh_ms
    broker = from_report(trade, now_ms, ea_version)
    tick = from_scalp_tick(scalp_tick, now_ms)
    cands = [c for c in (broker, tick) if c]
    if broker and broker["age_ms"] <= fresh_ms:
        best = broker
    elif tick and tick["age_ms"] <= fresh_ms and (not broker or tick["age_ms"] < broker["age_ms"]):
        best = tick
    else:
        best = min(cands, key=lambda c: c["age_ms"]) if cands else None
    if not best:
        return {"price": None, "bid": None, "ask": None, "age_ms": None, "source": None, "fresh": False,
                "used": None, "note": "no broker price reported for this position yet"}
    fresh = best["age_ms"] <= fresh_ms
    price, used = management_price(best, trade.get("action") or "")
    note = "" if used != "current_price" else "bid/ask not reported — managed on current_price"
    return {**best, "price": price, "used": used, "fresh": fresh, "note": note}


def health_for_trades(trades: list, now_ms: float | None = None) -> dict:
    """Bot Health: per-account max broker-price age across open live trades; amber when any > HEALTH_AMBER_MS."""
    out: dict = {}
    for t in trades:
        if (t.get("mode") or "live") == "paper" or t.get("status") != "open" or not t.get("mt5_ticket"):
            continue
        snap = from_report(t, now_ms)
        acc = str(t.get("account_id") or "")
        row = out.setdefault(acc, {"open_trades": 0, "max_age_ms": None, "missing": 0, "amber": False})
        row["open_trades"] += 1
        if snap is None:
            row["missing"] += 1
            row["amber"] = True
            continue
        row["max_age_ms"] = max(row["max_age_ms"] or 0, snap["age_ms"])
        if snap["age_ms"] > HEALTH_AMBER_MS:
            row["amber"] = True
    return out
