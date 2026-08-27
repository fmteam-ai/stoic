"""Outcome Attribution (v56 §12/§13) — STOIC never learns "loss →
strategy bad". Every closed trade is decomposed into multi-category
contributions (weights sum to 1): the signal may have been right while
execution, the broker, infrastructure, news or correlation destroyed the
result. Categories: ALPHA_ERROR, REGIME_ERROR, TIMING_ERROR,
SIZING_ERROR, EXECUTION_ERROR, BROKER_ERROR, INFRASTRUCTURE_ERROR,
NEWS_SHOCK, CORRELATION_ERROR, NORMAL_VARIANCE. Rule-based v1 — every
outcome carries engine_version so heuristics can be recalibrated later.
`alpha_clean` marks outcomes safe for AI alpha learning (result not
dominated by execution/broker/infra/news noise)."""
import logging
import uuid
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("outcome.attribution")

ENGINE_VERSION = 1
CATEGORIES = ["ALPHA_ERROR", "REGIME_ERROR", "TIMING_ERROR",
              "SIZING_ERROR", "EXECUTION_ERROR", "BROKER_ERROR",
              "INFRASTRUCTURE_ERROR", "NEWS_SHOCK", "CORRELATION_ERROR",
              "NORMAL_VARIANCE"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dt(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _session(dt: datetime) -> str:
    h = dt.astimezone(timezone.utc).hour
    if 7 <= h < 12:
        return "london"
    if 12 <= h < 16:
        return "overlap"
    if 16 <= h < 21:
        return "newyork"
    return "asia"


def result_r(trade: dict) -> tuple[float, str]:
    """P&L in R units. Prefer price-based R (planned risk vs realized
    move); fall back to pnl sign when price data is incomplete."""
    try:
        entry = float(trade.get("entry_price") or 0)
        sl = float(trade.get("stop_loss") or 0)
        exitp = float(trade.get("exit_price") or 0)
    except (TypeError, ValueError):
        entry = sl = exitp = 0
    if entry and sl and exitp and entry != sl:
        risk = abs(entry - sl)
        move = (exitp - entry) if str(trade.get("action")).upper() == "BUY" \
            else (entry - exitp)
        return round(max(-10.0, min(10.0, move / risk)), 3), "price"
    pnl = float(trade.get("pnl") or 0)
    if pnl:
        return (0.5 if pnl > 0 else -0.5), "pnl_sign"
    return 0.0, "flat"


async def _news_hits(db, trade: dict) -> int:
    """High-impact calendar events inside the trade window that touch the
    traded symbol's currencies. Cache-only — never triggers a fetch."""
    cache = await db.pamm_news_cache.find_one({"_id": "ff_thisweek"})
    if not cache:
        return 0
    opened, closed = _dt(trade.get("opened_at")), _dt(trade.get("closed_at"))
    if not opened or not closed:
        return 0
    lo = opened - timedelta(minutes=15)
    hi = closed + timedelta(minutes=5)
    sym = str(trade.get("symbol") or "").upper()
    ccys = {sym[:3], sym[-3:], "USD" if sym.startswith("XAU") else ""}
    hits = 0
    for e in cache.get("events") or []:
        if str(e.get("impact") or "").lower() not in ("high", "red"):
            continue
        if str(e.get("currency") or e.get("country") or "").upper() not in ccys:
            continue
        t = _dt(e.get("date") or e.get("time"))
        if t and lo <= t <= hi:
            hits += 1
    return hits


async def _signals(db, trade: dict) -> dict:
    s = {}
    entry = float(trade.get("entry_price") or 0)
    sl = float(trade.get("stop_loss") or 0)
    risk = abs(entry - sl) if entry and sl else 0.0
    slippage = abs(float(trade.get("slippage") or 0))
    s["slippage_ratio"] = round(slippage / risk, 3) if (slippage and risk) \
        else (0.1 if slippage else 0.0)
    opened = _dt(trade.get("opened_at"))
    confirmed = _dt(trade.get("broker_confirmed_at")) or _dt(
        trade.get("dispatched_at"))
    s["fill_delay_s"] = round((confirmed - opened).total_seconds(), 1) \
        if (opened and confirmed and confirmed > opened) else 0.0
    s["dispatch_retries"] = max(0, int(trade.get("_dispatch_count") or 1) - 1)
    s["ghost_or_external"] = bool(trade.get("external_open")
                                  or trade.get("backfilled_from_snapshot"))
    intent = None
    if trade.get("execution_intent_id"):
        intent = await db.execution_intents.find_one(
            {"intent_id": trade["execution_intent_id"]}, {"status": 1})
    s["intent_degraded"] = bool(intent and intent.get("status")
                                in ("unknown", "expired"))
    err = str(trade.get("error") or "").lower()
    s["broker_reject_text"] = any(w in err for w in
                                  ("requote", "off quotes", "rejected",
                                   "no prices", "trade disabled"))
    opened_iso = str(trade.get("opened_at") or "")
    closed_iso = str(trade.get("closed_at") or "")
    s["broker_incident_overlap"] = bool(
        opened_iso and closed_iso
        and await db.pamm_incidents.find_one(
            {"type": {"$in": ["flatten_failed", "position_drift"]},
             "opened_at": {"$lte": closed_iso},
             "$or": [{"status": "open"},
                     {"resolved_at": {"$gte": opened_iso}}]},
            {"_id": 1}))
    s["news_hits"] = await _news_hits(db, trade)
    s["authority_reduced"] = bool(trade.get("authority_reduced")
                                  or trade.get("risk_reduced"))
    if opened_iso and closed_iso and trade.get("user_id"):
        s["concurrent_losers"] = await db.trades.count_documents(
            {"user_id": trade["user_id"], "status": "closed",
             "_id": {"$ne": trade.get("_id")}, "pnl": {"$lt": 0},
             "opened_at": {"$lt": closed_iso},
             "closed_at": {"$gt": opened_iso}})
    else:
        s["concurrent_losers"] = 0
    oc, cc = _dt(trade.get("opened_at")), _dt(trade.get("closed_at"))
    s["session_shift"] = bool(oc and cc and _session(oc) != _session(cc))
    return s


def _weights(sig: dict, r: float) -> dict:
    raw = {}
    if sig["slippage_ratio"]:
        raw["EXECUTION_ERROR"] = min(0.5, 0.1 + sig["slippage_ratio"])
    if sig["fill_delay_s"] > 30:
        raw["TIMING_ERROR"] = 0.2 if sig["fill_delay_s"] > 120 else 0.1
    infra = (0.15 * min(2, sig["dispatch_retries"])
             + (0.25 if sig["intent_degraded"] else 0)
             + (0.2 if sig["ghost_or_external"] else 0))
    if infra:
        raw["INFRASTRUCTURE_ERROR"] = min(0.5, infra)
    broker = ((0.3 if sig["broker_reject_text"] else 0)
              + (0.15 if sig["broker_incident_overlap"] else 0))
    if broker:
        raw["BROKER_ERROR"] = min(0.45, broker)
    if sig["news_hits"]:
        raw["NEWS_SHOCK"] = min(0.4, 0.2 + 0.1 * (sig["news_hits"] - 1))
    if sig["authority_reduced"]:
        raw["SIZING_ERROR"] = 0.15
    if sig["concurrent_losers"]:
        raw["CORRELATION_ERROR"] = min(0.3,
                                       0.1 * sig["concurrent_losers"])
    if sig["session_shift"] and r < 0:
        raw["REGIME_ERROR"] = 0.15
    total = sum(raw.values())
    if r < 0:  # loss — residual blame stays with the signal itself
        cap = 0.85
        if total > cap:
            raw = {k: v * cap / total for k, v in raw.items()}
            total = cap
        alpha = max(0.0, round(0.9 - total, 3))
        out = {**raw, "ALPHA_ERROR": alpha,
               "NORMAL_VARIANCE": max(0.0, round(1 - alpha - total, 3))}
    else:  # win/flat — anomalies noted, rest is normal variance
        cap = 0.4
        if total > cap:
            raw = {k: v * cap / total for k, v in raw.items()}
            total = cap
        out = {**raw, "NORMAL_VARIANCE": max(0.0, round(1 - total, 3))}
    return {k: round(v, 3) for k, v in out.items() if round(v, 3) > 0}


async def attribute_trade(db, trade: dict) -> dict:
    """Decompose one closed trade. Idempotent — upserts by trade_id."""
    trade_id = str(trade.get("_id") or trade.get("id"))
    r, r_source = result_r(trade)
    sig = await _signals(db, trade)
    attribution = _weights(sig, r)
    primary = max(attribution, key=attribution.get)
    noise = sum(attribution.get(c, 0) for c in
                ("EXECUTION_ERROR", "BROKER_ERROR",
                 "INFRASTRUCTURE_ERROR", "NEWS_SHOCK"))
    outcome = {"outcome_id": f"out_{uuid.uuid4().hex[:10]}",
               "trade_id": trade_id, "user_id": trade.get("user_id"),
               "account_id": trade.get("account_id"),
               "symbol": trade.get("symbol"),
               "strategy_id": str(trade.get("scope")
                                  or trade.get("origin") or "manual"),
               "result_r": r, "r_source": r_source,
               "pnl": float(trade.get("pnl") or 0),
               "closed_at": trade.get("closed_at"),
               "attribution": attribution, "primary_category": primary,
               "alpha_clean": noise <= 0.3, "signals": sig,
               "engine_version": ENGINE_VERSION, "at": _now()}
    await db.trade_outcomes.update_one(
        {"trade_id": trade_id}, {"$set": outcome}, upsert=True)
    try:
        from bson import ObjectId
        await db.trades.update_one(
            {"_id": ObjectId(trade_id)},
            {"$set": {"attribution": attribution,
                      "attribution_primary": primary,
                      "alpha_clean": outcome["alpha_clean"]}})
    except Exception:
        pass
    return outcome


async def attribute_trade_by_id(db, trade_id: str) -> dict | None:
    from bson import ObjectId
    try:
        trade = await db.trades.find_one({"_id": ObjectId(trade_id)})
    except Exception:
        return None
    if not trade or trade.get("status") != "closed":
        return None
    return await attribute_trade(db, trade)


async def attribute_missing(db, limit: int = 100,
                            query: dict | None = None) -> int:
    """Backfill sweep: closed trades without attribution."""
    n = 0
    cursor = db.trades.find({"status": "closed",
                             "closed_at": {"$ne": None},
                             "attribution": {"$exists": False},
                             **(query or {})},
                            limit=limit)
    async for trade in cursor:
        try:
            await attribute_trade(db, trade)
            n += 1
        except Exception as e:
            logger.warning("attribution failed for trade %s: %s",
                           trade.get("_id"), e)
            from bson import ObjectId as _O
            await db.trades.update_one(
                {"_id": trade["_id"]},
                {"$set": {"attribution": {"NORMAL_VARIANCE": 1.0},
                          "attribution_primary": "UNATTRIBUTED"}})
    return n


async def ensure_attribution_indexes(db) -> None:
    await db.trade_outcomes.create_index("trade_id", unique=True)
    await db.trade_outcomes.create_index([("user_id", 1),
                                          ("closed_at", -1)])
    await db.trade_outcomes.create_index([("strategy_id", 1),
                                          ("closed_at", -1)])
