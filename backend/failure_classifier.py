"""Autopilot #7 — deterministic failure classification for losing trades.

A losing trade is NOT automatically a strategy failure. Each loss is
classified into exactly one category so the fix is routed to the RIGHT
component instead of always blaming the entry model:

    data_failure            stale/missing data     → repair data quality
    operational_failure     platform lifecycle     → improve reliability
    execution_failure       slippage/spread/fills  → execution controls
    risk_failure            oversize/correlation   → portfolio risk
    regime_failure          wrong conditions       → regime gating
    signal_failure          model misread market   → entry model
    normal_statistical_loss valid setup, adverse   → no change
"""
import logging
from datetime import datetime, timedelta, timezone

from pip_utils import base_symbol

logger = logging.getLogger("failure-classifier")

CATEGORIES = ("data_failure", "operational_failure", "execution_failure",
              "risk_failure", "regime_failure", "signal_failure",
              "normal_statistical_loss")

ROUTE_FIX = {
    "data_failure": "fail closed and repair data quality / feeds",
    "operational_failure": "platform reliability (EA link, reconciliation, workers)",
    "execution_failure": "execution controls (timing, spread, slippage guards) — NOT the entry model",
    "risk_failure": "portfolio and position risk (sizing, correlated exposure caps)",
    "regime_failure": "regime gating — strategy traded unsuitable conditions",
    "signal_failure": "entry model (features, thresholds, regime selection) after enough evidence",
    "normal_statistical_loss": "no strategy change — losses are part of the distribution",
}


def _slip_threshold(symbol: str) -> float:
    s = base_symbol(symbol or "")
    if s.startswith(("XAU", "GOLD")):
        return 2.5
    if s.startswith(("BTC", "ETH")):
        return 20.0
    return 1.5


def classify_failure(trade: dict, sig: dict | None = None,
                     evaluation: dict | None = None,
                     event_types: list | None = None,
                     context: dict | None = None) -> dict:
    """Pure, deterministic, priority-ordered. First matching category wins."""
    sig = sig or {}
    ev = evaluation or {}
    events = event_types or []
    ctx = context or {}
    mistakes = ev.get("mistakes") or []

    # 1. data failure — the pipeline knew its inputs were unsafe
    if sig.get("pipeline_safe_to_execute") is False or trade.get("data_stale"):
        return _verdict("data_failure",
                        ["pipeline flagged unsafe-to-execute at signal time"
                         if sig.get("pipeline_safe_to_execute") is False
                         else "trade stamped data_stale"])

    # 2. operational failure — lifecycle/reconciliation incidents
    op_ev = []
    if trade.get("pnl_estimated") or trade.get("pnl_unknown"):
        op_ev.append("P&L was estimated/unknown — broker reconciliation gap")
    if trade.get("backfilled_at"):
        op_ev.append("trade required deep-sync backfill")
    reason = str(trade.get("close_reason") or "").lower()
    if "ghost" in reason or "reconcil" in reason:
        op_ev.append(f"lifecycle close_reason: {reason}")
    if any("reject" in str(e).lower() or "timeout" in str(e).lower()
           for e in events):
        op_ev.append("rejection/timeout events in trade lifecycle")
    if op_ev:
        return _verdict("operational_failure", op_ev)

    # 3. execution failure — the fill, not the idea
    ex_ev = []
    slip = trade.get("slippage_pips")
    if slip is not None and float(slip) >= _slip_threshold(trade.get("symbol")):
        ex_ev.append(f"slippage {float(slip):.1f} pips ≥ "
                     f"{_slip_threshold(trade.get('symbol')):.1f} threshold")
    if ctx.get("spread_expanded"):
        ex_ev.append("spread expanded well beyond typical at entry")
    placed_rr, designed_rr = ctx.get("placed_rr"), ctx.get("designed_rr")
    if placed_rr and designed_rr and placed_rr < 0.6 * designed_rr:
        ex_ev.append(f"placed R:R {placed_rr:.2f} collapsed vs designed "
                     f"{designed_rr:.2f}")
    if ex_ev:
        return _verdict("execution_failure", ex_ev)

    # 4. risk failure — size/exposure, not direction
    rk_ev = []
    try:
        if float(trade.get("risk_pct") or 0) >= 2.0:
            rk_ev.append(f"oversized: {float(trade['risk_pct']):.2f}% risk on one trade")
    except (TypeError, ValueError):
        pass
    if int(ctx.get("correlated_open") or 0) >= 3:
        rk_ev.append(f"{ctx['correlated_open']} accounts entered the same "
                     f"symbol+direction within 10min — correlated exposure")
    if "oversized_in_drawdown" in mistakes:
        rk_ev.append("sized up while in drawdown (self-eval)")
    if rk_ev:
        return _verdict("risk_failure", rk_ev)

    # 5. regime failure — right model, wrong market
    rg_ev = []
    if ctx.get("regime_edge_negative"):
        rg_ev.append(ctx.get("regime_edge_detail")
                     or "strategy has proven negative edge in this regime")
    if "traded_into_news_event" in mistakes:
        rg_ev.append("entered into a scheduled news event window")
    if "counter_trend_entry" in mistakes:
        rg_ev.append("counter-trend entry against session structure")
    if rg_ev:
        return _verdict("regime_failure", rg_ev)

    # 6. signal failure — the model misread the market
    sg_ev = []
    mfe_r = ev.get("mfe_r")
    if mfe_r is not None and float(mfe_r) < 0.15:
        sg_ev.append(f"price never worked ≥0.15R in favor (MFE {mfe_r}R) — "
                     f"direction was wrong from the start")
    if (sig.get("uncertainty") or {}).get("risk") == "HIGH":
        sg_ev.append("entered on a HIGH-uncertainty prediction")
    if "low_confidence_entry" in mistakes:
        sg_ev.append("low-confidence entry (self-eval)")
    if sg_ev:
        return _verdict("signal_failure", sg_ev)

    # 7. normal statistical loss
    return _verdict("normal_statistical_loss",
                    ["setup valid, execution clean, price moved against it"
                     + (f" (MFE {mfe_r}R before reversal)"
                        if mfe_r is not None else "")])


def _verdict(category: str, evidence: list) -> dict:
    return {"category": category, "evidence": evidence,
            "route_fix_to": ROUTE_FIX[category]}


async def classify_trade_failure(db, trade: dict) -> dict:
    """Gather evidence from DB then run the pure classifier. Fail-open to
    normal_statistical_loss on any gathering error."""
    from bson import ObjectId
    sig, evaluation, event_types, ctx = {}, {}, [], {}
    try:
        if trade.get("signal_id"):
            sig = await db.signals.find_one(
                {"_id": ObjectId(trade["signal_id"])}) or {}
    except Exception:  # noqa: BLE001
        sig = {}
    try:
        evaluation = await db.trade_evaluations.find_one(
            {"trade_id": str(trade["_id"])}) or {}
    except Exception:  # noqa: BLE001
        evaluation = {}
    try:
        event_types = await db.trade_events.distinct(
            "event_type", {"trade_id": str(trade["_id"])})
    except Exception:  # noqa: BLE001
        event_types = []
    try:
        ctx["correlated_open"] = await _correlated_open(db, trade)
    except Exception:  # noqa: BLE001
        pass
    try:
        neg = await _regime_edge_negative(db, trade)
        if neg:
            ctx["regime_edge_negative"] = True
            ctx["regime_edge_detail"] = neg
    except Exception:  # noqa: BLE001
        pass
    return classify_failure(trade, sig, evaluation, event_types, ctx)


async def _correlated_open(db, trade) -> int:
    opened = trade.get("opened_at")
    if not opened:
        return 0
    t0 = datetime.fromisoformat(str(opened).replace("Z", "+00:00"))
    if t0.tzinfo is None:
        t0 = t0.replace(tzinfo=timezone.utc)
    lo, hi = ((t0 - timedelta(minutes=10)).isoformat(),
              (t0 + timedelta(minutes=10)).isoformat())
    base = base_symbol(trade.get("symbol") or "")
    n = 0
    async for t in db.trades.find(
            {"user_id": trade["user_id"], "origin": "auto",
             "action": trade.get("action"),
             "opened_at": {"$gte": lo, "$lte": hi}},
            {"symbol": 1, "account_id": 1}).limit(20):
        if base_symbol(t.get("symbol") or "") == base:
            n += 1
    return n


async def _regime_edge_negative(db, trade) -> str | None:
    key = (trade.get("market_regime") or {}).get("key")
    cls = trade.get("strategy_class")
    if not key or not cls:
        return None
    since = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    pnls = [float(t["pnl"]) async for t in db.trades.find(
        {"user_id": trade["user_id"], "origin": "auto", "status": "closed",
         "pnl": {"$ne": None}, "closed_at": {"$gte": since},
         "strategy_class": cls, "market_regime.key": key},
        {"pnl": 1}).limit(500)]
    if len(pnls) >= 8:
        mean = sum(pnls) / len(pnls)
        if mean < 0:
            return (f"{cls} averages ${mean:.2f}/trade over {len(pnls)} "
                    f"trades in regime {key}")
    return None
