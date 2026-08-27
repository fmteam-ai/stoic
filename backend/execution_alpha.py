"""Execution Alpha Engine (v60 #9) — STOIC already decides WHAT to
trade; this decides HOW: EXECUTE_NOW / WAIT / REDUCE / SKIP (actionable)
plus LIMIT / SPLIT (advisory recommendations, learned against outcomes).
Inputs: live spread state, broker execution forecast, volatility &
liquidity from the market-state vector, order size vs the account's own
recent norm. Downscale-only: alpha never increases exposure."""
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("execution.alpha")

MODES = ["EXECUTE_NOW", "WAIT", "LIMIT", "MARKET", "SPLIT",
         "REDUCE", "SKIP"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def classify(*, spread_delay_ms: int, expected_slippage_pips,
             typical_spread_pips, fill_grade, volatility, liquidity,
             lot: float, median_lot, is_scalp: bool) -> dict:
    """Pure execution-plan classifier."""
    mode, mult, reasons, advisory = "EXECUTE_NOW", 1.0, [], []
    if (is_scalp and expected_slippage_pips is not None
            and typical_spread_pips
            and expected_slippage_pips > 2.0 * typical_spread_pips):
        return {"mode": "SKIP", "risk_multiplier": 0.0,
                "reasons": [f"expected slippage "
                            f"{expected_slippage_pips}p > 2× typical "
                            f"spread {typical_spread_pips:.1f}p — scalp "
                            f"edge destroyed by execution cost"],
                "advisory": []}
    if spread_delay_ms > 0:
        mode = "WAIT"
        reasons.append(f"transient spread widening — wait up to "
                       f"{spread_delay_ms}ms for reversion")
    if fill_grade == "D":
        mult = 0.7
        if mode == "EXECUTE_NOW":
            mode = "REDUCE"
        reasons.append("broker fill-quality grade D — size reduced 0.7x")
    if median_lot and lot >= 2.0 * float(median_lot):
        advisory.append({"recommendation": "SPLIT",
                         "tranches": 2,
                         "reason": f"order {lot} lots ≥ 2× recent median "
                                   f"{median_lot} — splitting reduces "
                                   f"market impact"})
    try:
        v = float(volatility) if volatility is not None else None
        liq = float(liquidity) if liquidity is not None else None
    except (TypeError, ValueError):
        v = liq = None
    if v is not None and liq is not None and v >= 0.85 and liq < 0.3:
        advisory.append({"recommendation": "LIMIT",
                         "reason": "extreme volatility on a thin book — "
                                   "a limit at entry avoids paying the "
                                   "panic spread"})
    if not reasons:
        reasons.append("conditions normal — execute at market now")
    return {"mode": mode, "risk_multiplier": mult, "reasons": reasons,
            "advisory": advisory}


async def _median_recent_lot(db, account_id: str):
    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    lots = []
    async for t in db.trades.find(
            {"account_id": account_id, "origin": "auto",
             "created_at": {"$gte": since}},
            {"lot_size": 1}).sort("created_at", -1).limit(100):
        if t.get("lot_size"):
            lots.append(float(t["lot_size"]))
    if len(lots) < 5:
        return None
    lots.sort()
    return lots[len(lots) // 2]


async def decide(db, user_id: str, account: dict, signal: dict,
                 lot: float) -> dict:
    """Full plan: gather evidence → classify → persist for learning."""
    acc_id = str(account["_id"])
    sym = str(signal.get("symbol") or "")
    scope = str(signal.get("scope") or "")
    from broker_intel import _typical_spread_pips, execution_forecast
    from execution_timing import decide as spread_decide
    sp = spread_decide(acc_id, sym)
    try:
        fc = await execution_forecast(db, account, symbol=sym)
    except Exception as e:  # noqa: BLE001
        logger.debug("execution forecast failed: %s", e)
        fc = {}
    vec = (signal.get("market_state") or {}).get("vector") or {}
    plan = classify(
        spread_delay_ms=int(sp.get("delay_ms") or 0),
        expected_slippage_pips=fc.get("expected_slippage_pips"),
        typical_spread_pips=_typical_spread_pips(sym),
        fill_grade=fc.get("fill_quality_grade"),
        volatility=vec.get("volatility"), liquidity=vec.get("liquidity"),
        lot=float(lot), median_lot=await _median_recent_lot(db, acc_id),
        is_scalp="scalp" in scope.lower())
    plan.update({"forecast": {k: fc.get(k) for k in
                              ("expected_slippage_pips",
                               "expected_latency_ms",
                               "fill_quality_grade", "broker_score")},
                 "spread_state": {k: sp.get(k) for k in
                                  ("delay_ms", "spread_now",
                                   "spread_median")},
                 "engine_version": 1, "at": _now()})
    try:
        await db.execution_alpha_decisions.insert_one(
            {"user_id": user_id, "account_id": acc_id, "symbol": sym,
             "scope": scope, "decision_id": signal.get("decision_id"),
             "lot": float(lot), **plan})
        plan.pop("_id", None)
    except Exception as e:  # noqa: BLE001
        logger.debug("alpha decision persist failed: %s", e)
    return plan
