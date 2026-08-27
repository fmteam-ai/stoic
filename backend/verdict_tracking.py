"""Verdict Outcome Tracking — every risk decision that reduces or blocks
size is recorded with its eventual result (real for REDUCE via the linked
trade, counterfactual price-path simulation for REJECT/BLOCK) so the
scaling curves prove their worth empirically: money saved vs opportunity
cost, per limiting factor and per curve."""
import logging
import uuid
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("verdict.tracking")

BLOCKED_TIMEOUT_H = 48   # counterfactual scoring window for blocked trades
MAX_TICKS = 5000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dt(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


async def record_verdict(db, *, source: str, verdict: str,
                         requested: float, approved: float,
                         unit: str = "risk_pct", user_id=None,
                         program_id=None, limiting_factor=None,
                         factors=None, reasons=None, nav=None,
                         context=None) -> str:
    requested = float(requested or 0)
    approved = float(approved or 0)
    vid = f"vd_{uuid.uuid4().hex[:12]}"
    await db.risk_verdicts.insert_one({
        "verdict_id": vid, "source": source, "verdict": verdict,
        "requested": requested, "approved": approved, "unit": unit,
        "reduction_pct": round(100 * (1 - approved / requested), 2)
        if requested else 100.0,
        "user_id": user_id, "program_id": program_id,
        "limiting_factor": limiting_factor, "factors": factors or [],
        "reasons": reasons or [], "nav": nav, "context": context or {},
        "trade_id": None, "status": "pending", "resolution": None,
        "created_at": _now()})
    return vid


async def link_trade(db, verdict_id: str, trade_id) -> None:
    await db.risk_verdicts.update_one(
        {"verdict_id": verdict_id}, {"$set": {"trade_id": str(trade_id)}})


async def resolve_with_trade(db, trade: dict) -> dict | None:
    """REDUCE verdicts — score against the real closed trade: what would
    the full-size P&L have been vs what was actually taken."""
    trade_id = str(trade.get("_id") or trade.get("id"))
    v = await db.risk_verdicts.find_one({"trade_id": trade_id,
                                         "status": "pending"})
    if not v:
        return None
    pnl = float(trade.get("pnl") or 0)
    factor = (v["approved"] / v["requested"]) if v.get("requested") else 1.0
    factor = factor if factor > 0 else 1.0
    full = pnl / factor
    delta = round(full - pnl, 2)   # the P&L NOT taken due to the reduction
    saved = round(max(0.0, -delta), 2)
    cost = round(max(0.0, delta), 2)
    res = {"kind": "real", "pnl": round(pnl, 2),
           "hypothetical_full_pnl": round(full, 2),
           "saved_usd": saved, "opportunity_cost_usd": cost,
           "net_benefit_usd": round(saved - cost, 2), "resolved_at": _now()}
    await db.risk_verdicts.update_one(
        {"verdict_id": v["verdict_id"]},
        {"$set": {"status": "resolved", "resolution": res}})
    return res


async def resolve_for_trade(db, trade_id: str) -> dict | None:
    from bson import ObjectId
    try:
        t = await db.trades.find_one({"_id": ObjectId(trade_id)})
    except Exception:
        return None
    if not t or t.get("status") != "closed":
        return None
    return await resolve_with_trade(db, t)


def _ctx_scoreable(ctx: dict) -> bool:
    try:
        return bool(ctx.get("symbol")
                    and float(ctx.get("entry_price") or 0)
                    and float(ctx.get("stop_loss") or 0))
    except (TypeError, ValueError):
        return False


async def _counterfactual_r(db, v: dict, ctx: dict):
    """Follow persisted price ticks from verdict time. Returns
    (result_r, kind) once resolvable, None while still running."""
    entry = float(ctx["entry_price"])
    sl = float(ctx["stop_loss"])
    tp = float(ctx.get("take_profit") or 0)
    side = str(ctx.get("side") or "BUY").upper()
    risk = abs(entry - sl)
    if not risk:
        return 0.0, "counterfactual_invalid"
    reward_r = round(abs(tp - entry) / risk, 3) if tp else 1.0
    created_dt = _dt(v.get("created_at")) or datetime.now(timezone.utc)
    cur = db.price_ticks.find(
        {"symbol": str(ctx["symbol"]).upper(),
         "ts": {"$gte": created_dt}}).sort("ts", 1).limit(MAX_TICKS)
    last_price = None
    async for t in cur:
        p = float(t.get("price") or 0)
        if not p:
            continue
        last_price = p
        if side == "BUY":
            if p <= sl:
                return -1.0, "counterfactual_stop"
            if tp and p >= tp:
                return reward_r, "counterfactual_target"
        else:
            if p >= sl:
                return -1.0, "counterfactual_stop"
            if tp and p <= tp:
                return reward_r, "counterfactual_target"
    age_h = (datetime.now(timezone.utc) - created_dt).total_seconds() / 3600
    if age_h < BLOCKED_TIMEOUT_H:
        return None
    if last_price is None:
        return 0.0, "counterfactual_no_data"
    move = (last_price - entry) if side == "BUY" else (entry - last_price)
    return round(max(-1.0, min(reward_r, move / risk)), 3), \
        "counterfactual_timeout"


async def resolve_blocked(db, limit: int = 50) -> int:
    """Sweep pending REJECT/BLOCK verdicts and score the counterfactual:
    would the blocked trade have hit its stop (money saved) or its target
    (opportunity cost)?"""
    n = 0
    cursor = db.risk_verdicts.find(
        {"status": "pending", "trade_id": None,
         "verdict": {"$in": ["REJECT", "BLOCK"]}}).limit(limit)
    async for v in cursor:
        ctx = v.get("context") or {}
        if not _ctx_scoreable(ctx):
            created_dt = _dt(v.get("created_at"))
            if created_dt and (datetime.now(timezone.utc) - created_dt
                               ).total_seconds() / 3600 >= BLOCKED_TIMEOUT_H:
                await db.risk_verdicts.update_one(
                    {"verdict_id": v["verdict_id"]},
                    {"$set": {"status": "unscoreable",
                              "resolution": {"kind": "no_context",
                                             "resolved_at": _now()}}})
            continue
        try:
            out = await _counterfactual_r(db, v, ctx)
        except Exception as e:
            logger.warning("counterfactual failed for %s: %s",
                           v.get("verdict_id"), e)
            continue
        if out is None:
            continue
        r_val, kind = out
        risk_amt = None
        if v.get("unit") == "risk_pct" and v.get("nav") and v.get("requested"):
            risk_amt = float(v["nav"]) * float(v["requested"]) / 100.0
        hyp = round(r_val * risk_amt, 2) if risk_amt is not None else None
        saved = round(max(0.0, -(hyp or 0)), 2)
        cost = round(max(0.0, hyp or 0), 2)
        res = {"kind": kind, "result_r": r_val, "hypothetical_pnl": hyp,
               "saved_usd": saved, "opportunity_cost_usd": cost,
               "net_benefit_usd": round(saved - cost, 2),
               "resolved_at": _now()}
        await db.risk_verdicts.update_one(
            {"verdict_id": v["verdict_id"]},
            {"$set": {"status": "resolved", "resolution": res}})
        n += 1
    return n


async def effectiveness_summary(db, days: int = 30,
                                user_id: str | None = None) -> dict:
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=max(1, min(int(days), 365)))).isoformat()
    q = {"created_at": {"$gte": cutoff}}
    if user_id:
        q["user_id"] = user_id
    totals = {"verdicts": 0, "resolved": 0, "pending": 0, "unscoreable": 0,
              "saved_usd": 0.0, "opportunity_cost_usd": 0.0}
    by_verdict: dict = {}
    by_factor: dict = {}
    async for v in db.risk_verdicts.find(q, {"_id": 0}).limit(5000):
        totals["verdicts"] += 1
        st = v.get("status")
        if st == "resolved":
            totals["resolved"] += 1
        elif st == "unscoreable":
            totals["unscoreable"] += 1
        else:
            totals["pending"] += 1
        res = v.get("resolution") or {}
        saved = float(res.get("saved_usd") or 0)
        cost = float(res.get("opportunity_cost_usd") or 0)
        totals["saved_usd"] += saved
        totals["opportunity_cost_usd"] += cost
        bv = by_verdict.setdefault(v.get("verdict") or "?",
                                   {"count": 0, "resolved": 0,
                                    "saved_usd": 0.0,
                                    "opportunity_cost_usd": 0.0})
        bv["count"] += 1
        if st == "resolved":
            bv["resolved"] += 1
        bv["saved_usd"] += saved
        bv["opportunity_cost_usd"] += cost
        lf = v.get("limiting_factor") or "unspecified"
        bf = by_factor.setdefault(lf, {"count": 0, "resolved": 0,
                                       "saved_usd": 0.0,
                                       "opportunity_cost_usd": 0.0})
        bf["count"] += 1
        if st == "resolved":
            bf["resolved"] += 1
        bf["saved_usd"] += saved
        bf["opportunity_cost_usd"] += cost
    for d in ([totals] + list(by_verdict.values())
              + list(by_factor.values())):
        d["saved_usd"] = round(d["saved_usd"], 2)
        d["opportunity_cost_usd"] = round(d["opportunity_cost_usd"], 2)
        d["net_benefit_usd"] = round(d["saved_usd"]
                                     - d["opportunity_cost_usd"], 2)
    return {"days": days, "totals": totals, "by_verdict": by_verdict,
            "by_limiting_factor": by_factor,
            "lesson": ("Risk reductions are net "
                       + ("SAVING money — the curves are earning their keep."
                          if totals["net_benefit_usd"] >= 0 else
                          "COSTING opportunity — review the tightest "
                          "limiting factors."))
            if totals["resolved"] else
            "No resolved verdicts yet — reductions are scored when the "
            "trade closes; blocks via counterfactual price simulation."}


async def ensure_verdict_indexes(db) -> None:
    await db.risk_verdicts.create_index("verdict_id", unique=True)
    await db.risk_verdicts.create_index([("status", 1), ("verdict", 1)])
    await db.risk_verdicts.create_index("trade_id")
    await db.risk_verdicts.create_index([("user_id", 1),
                                         ("created_at", -1)])
