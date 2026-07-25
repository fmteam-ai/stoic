"""Tier 1 — Decision DNA: one consolidated, signed record per trade.

Answers the four governance questions with recorded evidence:
why decided · what evidence · what risks considered · outcome vs expectation.
HMAC-signed (same attestation chain as verified performance) so the record
is tamper-evident and replayable.
"""
from adaptive_sizing import spread_mult


async def compose_dna(db, trade: dict) -> dict:
    from bson import ObjectId
    sig = {}
    if trade.get("signal_id"):
        try:
            sig = await db.signals.find_one(
                {"_id": ObjectId(trade["signal_id"])}) or {}
        except Exception:  # noqa: BLE001
            sig = {}
    ev = await db.trade_evaluations.find_one(
        {"trade_id": str(trade["_id"])}) or {}
    lr = await db.learning_records.find_one(
        {"trade_id": str(trade["_id"])}) or {}
    broker = await db.broker_intel_scores.find_one(
        {"account_id": trade.get("account_id")}, sort=[("at", -1)]) or {}

    mc = sig.get("monte_carlo") or {}
    unc = sig.get("uncertainty") or {}
    cons = sig.get("consensus") or {}
    ts = (sig.get("trend_score") or {}).get("quality") or {}
    spread = sig.get("spread") or (sig.get("execution") or {}).get("spread")
    regime = trade.get("market_regime") or sig.get("regime")

    dna = {
        "trade_id": str(trade["_id"]),
        "symbol": trade.get("symbol"), "action": trade.get("action"),
        "market_regime": regime,
        "trend_strength": {"score": ts.get("score"),
                           "direction": ts.get("direction"),
                           "components": ts.get("components")},
        "volatility_score": (ts.get("components") or {}).get(
            "volatility_support"),
        "liquidity_score": (max(0, min(100, round(
            (spread_mult(spread, trade.get("symbol")) - 0.55) / 0.45 * 100)))
                            if spread is not None else None),
        "news_score": {"net": (sig.get("news_ai") or {}).get("net"),
                       "calendar": (sig.get("calendar_intel") or {}).get("event")},
        "confidence": {"raw": sig.get("confidence"),
                       "calibrated": unc.get("confidence_pct"),
                       "uncertainty_tier": unc.get("risk")},
        "expected_value_r": mc.get("ev_r_net", mc.get("ev_r")),
        "risk_budget": {"risk_pct": trade.get("risk_pct"),
                        "lot_size": trade.get("lot_size"),
                        "sizing_method": trade.get("sizing_method")
                        or sig.get("sizing_method")},
        "strategy_version": trade.get("versions") or sig.get("versions"),
        "ai_opinion": (str(sig.get("reasoning") or "")[:500] or None),
        "deterministic_opinion": {"consensus_score": cons.get("score"),
                                  "engine": sig.get("strategy_engine")
                                  or trade.get("scope")},
        "execution_delay_ms": lr.get("latency_ms"),
        "broker_quality": {"score": broker.get("score"),
                           "broker": broker.get("broker"),
                           "components": broker.get("components")},
        "exit_logic": {"stop_loss": trade.get("stop_loss"),
                       "take_profit": trade.get("take_profit"),
                       "close_reason": trade.get("close_reason")},
        "learning_record": {"mfe_r": ev.get("mfe_r"),
                            "mae_r": ev.get("mae_r"),
                            "realized_r": ev.get("realized_r"),
                            "failure": lr.get("failure")},
    }

    expected = dna["expected_value_r"]
    realized = ev.get("realized_r")
    dna["four_questions"] = {
        "why_decided": (
            f"{trade.get('action')} {trade.get('symbol')} — signal record "
            "unavailable (older trade)" if not sig else
            f"{trade.get('action')} {trade.get('symbol')} via "
            f"{dna['deterministic_opinion']['engine'] or 'engine'} at "
            f"{sig.get('confidence') if sig.get('confidence') is not None else '—'}% confidence in regime "
            f"{((regime or {}).get('key') if isinstance(regime, dict) else regime) or '—'}"),
        "evidence": {
            "consensus_score": cons.get("score"),
            "expected_value_r": expected,
            "trend_quality": ts.get("score"),
            "calibrated_confidence": unc.get("confidence_pct"),
            "news_context": (sig.get("news_ai") or {}).get("net")},
        "risks_considered": {
            "risk_pct": trade.get("risk_pct"),
            "uncertainty_tier": unc.get("risk"),
            "stop_distance": (abs((trade.get("entry_price") or 0)
                                  - (trade.get("stop_loss") or 0))
                              if trade.get("stop_loss") else None),
            "broker_quality": broker.get("score")},
        "outcome_vs_expectation": {
            "expected_r": expected, "realized_r": realized,
            "verdict": ("pending" if trade.get("status") != "closed"
                        else "beat expectation"
                        if (expected is not None and realized is not None
                            and realized >= expected)
                        else "below expectation"
                        if (expected is not None and realized is not None)
                        else "unmeasured")},
    }
    from differentiation import perf_attestation
    dna["attestation"] = perf_attestation(dna)
    return dna
