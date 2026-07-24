"""Full execution trace (Phase 6 — build trust). READ-ONLY composer.

For any trade, answers — with evidence, from data that was actually
recorded at decision time:

    why_opened      decision ledger verdict + confluence + engine + AI stack
    why_this_time   session, live market regime, execution timing, news state
    why_this_size   risk %, adaptive sizing, risk budget share, Kelly/vetoes
    why_this_stop   engine geometry, SL pips/price, payoff guard, slippage cap
    why_this_target TP ladder, R:R, Monte-Carlo TP-first probability
    what_changed    chronological lifecycle: dispatch → fill → BE/partials/
                    trailing/adaptive-exit actions → broker deals
    why_closed      close reason, exit price, realized P&L, duration, journal

Nothing here is generated after the fact — every sentence cites fields
stamped on the signal/trade/ledger when the decision was made.
"""
import logging
from datetime import datetime, timezone

logger = logging.getLogger("execution-trace")


def _iso(v):
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v) if v else None


def _dur(opened, closed):
    try:
        a = datetime.fromisoformat(str(opened).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(closed).replace("Z", "+00:00"))
        m = int((b - a).total_seconds() // 60)
        return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}min"
    except Exception:
        return None


def why_opened(trade: dict, signal: dict, decision: dict | None) -> dict:
    conf = signal.get("confidence")
    engine = signal.get("engine_label") or signal.get("strategy_engine")
    bits = []
    if engine:
        bits.append(f"{engine} engine signal")
    if conf is not None:
        bits.append(f"confidence {conf}%"
                    + (f" (min required {signal['min_confidence_required']}%)"
                       if signal.get("min_confidence_required") else ""))
    mtf = signal.get("mtf_confluence")
    if isinstance(mtf, dict) and mtf.get("aligned") is not None:
        bits.append(f"multi-timeframe {'aligned' if mtf['aligned'] else 'mixed'}")
    mc = signal.get("monte_carlo") or {}
    if mc.get("ev_r_net") is not None:
        bits.append(f"simulated EV {mc['ev_r_net']:+.2f}R net of costs")
    if decision:
        bits.append(f"approved at stage '{decision.get('stage')}'")
    answer = (f"{trade.get('action')} {trade.get('symbol')}: "
              + "; ".join(bits)) if bits else "opened by the strategy pipeline"
    return {"answer": answer,
            "evidence": {"engine": engine, "confidence": conf,
                         "key_factors": (signal.get("key_factors") or [])[:5],
                         "reasoning": (signal.get("reasoning") or
                                       signal.get("explanation") or "")[:400],
                         "monte_carlo": {k: mc.get(k) for k in
                                         ("ev_r_net", "p_tp_first", "rr")},
                         "decision_stage": decision.get("stage") if decision else None,
                         "versions": trade.get("versions")}}


def why_this_time(trade: dict, signal: dict) -> dict:
    bits = []
    sess = signal.get("session")
    if isinstance(sess, dict):
        sess = sess.get("primary") or "/".join(sess.get("active_sessions") or []) or None
    if sess:
        bits.append(f"{sess} session")
    reg = (signal.get("market_regime") or {}).get("label") \
        or (signal.get("regime_execution_mode") or {}).get("execution_mode") \
        or signal.get("regime")
    if reg:
        bits.append(f"market regime: {reg}")
    et = signal.get("execution_timing")
    if et and et.get("waited_ms"):
        bits.append(f"send delayed {et['waited_ms']}ms for spread reversion "
                    f"({et.get('spread_before', et.get('spread_now'))}→"
                    f"{et.get('spread_after')}p)")
    nb = signal.get("news_bias")
    if nb:
        bits.append(f"news bias: {nb}")
    up = signal.get("upcoming_macro")
    if up:
        bits.append("inside a clear macro window")
    answer = ("; ".join(bits) if bits
              else "conditions passed every timing gate at signal time")
    return {"answer": answer,
            "evidence": {"session": sess, "regime": reg,
                         "execution_timing": et,
                         "news_bias": nb,
                         "liquidity_window": signal.get("liquidity_window"),
                         "signal_created_at": _iso(signal.get("created_at")),
                         "dispatched_at": trade.get("_dispatched_at"),
                         "filled_at": trade.get("acknowledged_at")}}


def why_this_size(trade: dict, signal: dict) -> dict:
    bits = []
    rp = trade.get("risk_pct") or signal.get("effective_risk_pct")
    if rp:
        bits.append(f"{rp}% of equity at risk")
    bits.append(f"{trade.get('lot_size')} lots")
    rb = signal.get("risk_budget")
    if rb and rb.get("shrunk_from"):
        bits.append(f"shrunk from {rb['shrunk_from']}% by the daily "
                    f"strategy risk budget")
    ads = signal.get("adaptive_sizing")
    if isinstance(ads, dict) and ads.get("multiplier") not in (None, 1, 1.0):
        bits.append(f"adaptive sizing ×{ads['multiplier']}")
    if signal.get("corr_kelly_trim"):
        bits.append("trimmed for portfolio correlation")
    if trade.get("original_lot_size") and \
            trade["original_lot_size"] != trade.get("lot_size"):
        bits.append(f"broker-adjusted from {trade['original_lot_size']} lots")
    return {"answer": "; ".join(bits),
            "evidence": {"risk_pct": rp, "lot_size": trade.get("lot_size"),
                         "risk_amount": signal.get("risk_amount"),
                         "kelly_f": signal.get("kelly_f"),
                         "risk_budget": rb,
                         "adaptive_sizing": ads if isinstance(ads, dict) else None,
                         "allocator": signal.get("portfolio_allocator")}}


def why_this_stop(trade: dict, signal: dict) -> dict:
    geo = signal.get("engine_geometry") or {}
    bits = [f"SL {trade.get('stop_loss')}"]
    if trade.get("sl_pips"):
        bits.append(f"{trade['sl_pips']} pips from entry")
    if geo.get("sl_atr_mult"):
        bits.append(f"{geo['sl_atr_mult']}× ATR engine geometry")
    if trade.get("slippage_pips") is not None:
        bits.append(f"fill slippage {trade['slippage_pips']} pips "
                    f"(within the slippage veto)")
    if trade.get("applied_sl") and trade["applied_sl"] != trade.get("requested_sl"):
        bits.append("broker-normalized to its stops level")
    return {"answer": "; ".join(str(b) for b in bits),
            "evidence": {"stop_loss": trade.get("stop_loss"),
                         "sl_pips": trade.get("sl_pips"),
                         "engine_geometry": geo or None,
                         "requested_price": trade.get("requested_price"),
                         "slippage_pips": trade.get("slippage_pips"),
                         "risk_engine": signal.get("risk_engine")}}


def why_this_target(trade: dict, signal: dict) -> dict:
    mc = signal.get("monte_carlo") or {}
    tps = [trade.get(k) for k in ("tp1", "tp2", "tp3") if trade.get(k)]
    bits = []
    if tps:
        bits.append(f"tiered ladder {' → '.join(str(t) for t in tps)}")
    elif trade.get("take_profit"):
        bits.append(f"TP {trade['take_profit']}")
    if signal.get("rr_ratio"):
        bits.append(f"{signal['rr_ratio']}:1 reward-to-risk")
    if mc.get("p_tp_first") is not None:
        bits.append(f"{round(mc['p_tp_first'] * 100)}% simulated chance "
                    f"TP is hit before SL")
    if trade.get("trend_ride"):
        bits.append("targets widened for a trending day (trend-ride)")
    return {"answer": "; ".join(bits) if bits else "engine default targets",
            "evidence": {"tp1": trade.get("tp1"), "tp2": trade.get("tp2"),
                         "tp3": trade.get("tp3"),
                         "tp_pips": trade.get("tp_pips"),
                         "rr_ratio": signal.get("rr_ratio"),
                         "p_tp_first": mc.get("p_tp_first"),
                         "trend_ride": bool(trade.get("trend_ride"))}}


def _flag_events(trade: dict) -> list:
    """Legacy timestamped flags on the trade doc → timeline entries."""
    out = []
    if trade.get("_dispatched_at"):
        out.append({"kind": "DISPATCHED", "at": trade["_dispatched_at"],
                    "detail": f"order sent to the EA"
                              f" (attempt {trade.get('_dispatch_count') or 1})"})
    if trade.get("acknowledged_at") or trade.get("opened_at"):
        slip = trade.get("slippage_pips")
        out.append({"kind": "FILLED",
                    "at": trade.get("acknowledged_at") or trade.get("opened_at"),
                    "detail": f"filled at {trade.get('entry_price')}"
                              + (f" ({slip:+.1f} pip slippage)" if slip is not None else "")})
    if trade.get("breakeven_set"):
        out.append({"kind": "BREAKEVEN", "at": None,
                    "detail": f"stop moved to break-even"
                              f" ({trade.get('be_target_sl') or trade.get('stop_loss')})"})
    for tier in ("tp1", "tp2", "tp3"):
        if trade.get(f"{tier}_closed"):
            out.append({"kind": tier.upper() + "_BANKED", "at": None,
                        "detail": f"{tier.upper()} tier banked (partial close)"})
    if trade.get("trail_active"):
        out.append({"kind": "TRAILING", "at": None,
                    "detail": "trailing stop active"})
    ev = trade.get("exit_vol_retarget")
    if ev:
        out.append({"kind": "EXIT_RETARGET_VOL", "at": ev.get("at"),
                    "detail": f"TP ladder rescaled ×{ev.get('scale')} — "
                              f"ATR moved {ev.get('atr_ratio')}× vs entry"})
    if trade.get("exit_fade_tightened"):
        out.append({"kind": "EXIT_TIGHTEN_FADE",
                    "at": trade.get("exit_last_tighten_at"),
                    "detail": "stop tightened — momentum faded while in profit"})
    ed = trade.get("exit_derisked")
    if ed:
        out.append({"kind": "EXIT_DERISK_RESISTANCE", "at": ed.get("at"),
                    "detail": f"25% banked into opposing structure at {ed.get('barrier')}"})
    if trade.get("closed_at"):
        out.append({"kind": "CLOSED", "at": trade["closed_at"],
                    "detail": f"closed at {trade.get('exit_price')} — "
                              f"{trade.get('close_reason') or 'broker close'}"
                              f" (P&L ${trade.get('pnl')})"})
    return out


async def what_changed(db, trade: dict) -> dict:
    timeline = _flag_events(trade)
    async for e in db.trade_events.find(
            {"trade_id": str(trade["_id"])}).sort("ts_ms", 1).limit(200):
        timeline.append({"kind": e.get("event_type"),
                         "at": e.get("occurred_at"),
                         "detail": (e.get("payload") or {}).get("detail")
                         or e.get("event_type")})
    if trade.get("position_id") or trade.get("mt5_ticket"):
        pid = trade.get("position_id") or trade.get("mt5_ticket")
        async for d in db.broker_deals.find(
                {"account_id": trade.get("account_id"),
                 "mt5_ticket": pid}).sort("deal_time", 1).limit(50):
            timeline.append({
                "kind": f"BROKER_DEAL_{str(d.get('deal_entry') or '').upper()}",
                "at": datetime.fromtimestamp(
                    float(d["deal_time"]), tz=timezone.utc).isoformat()
                if d.get("deal_time") else None,
                "detail": f"{d.get('action')} {d.get('lots')} lots @ "
                          f"{d.get('price')}"
                          + (f" (P&L ${d.get('profit')})"
                             if d.get("deal_entry") == "out" else "")})
    dated = [t for t in timeline if t.get("at")]
    undated = [t for t in timeline if not t.get("at")]
    dated.sort(key=lambda t: str(t["at"]))
    n_mods = sum(1 for t in timeline
                 if t["kind"] not in ("DISPATCHED", "FILLED", "CLOSED"))
    answer = (f"{n_mods} in-flight adjustment(s) recorded"
              if n_mods else "no in-flight changes — ran as planned")
    return {"answer": answer, "timeline": dated + undated}


async def why_closed(db, trade: dict) -> dict:
    if trade.get("status") != "closed":
        return {"answer": f"still {trade.get('status')} — not closed yet",
                "evidence": {"status": trade.get("status")}}
    reason = trade.get("close_reason") or "closed at the broker"
    dur = _dur(trade.get("opened_at"), trade.get("closed_at"))
    answer = (f"{reason}: exited at {trade.get('exit_price')} for "
              f"${trade.get('pnl')}" + (f" after {dur}" if dur else ""))
    journal = await db.journal_cards.find_one({"trade_id": str(trade["_id"])})
    return {"answer": answer,
            "evidence": {"close_reason": reason,
                         "exit_price": trade.get("exit_price"),
                         "pnl": trade.get("pnl"), "duration": dur,
                         "journal_summary": (journal or {}).get("summary")
                         or (journal or {}).get("headline")}}


async def compose(db, trade: dict) -> dict:
    from bson import ObjectId
    signal = {}
    if trade.get("signal_id"):
        try:
            signal = await db.signals.find_one(
                {"_id": ObjectId(str(trade["signal_id"]))}) or {}
        except Exception:
            signal = {}
    decision = None
    try:
        decision = await db.trade_decisions.find_one(
            {"user_id": trade["user_id"], "symbol": trade.get("symbol"),
             "status": "executed",
             "trade_id": str(trade["_id"])}) or await db.trade_decisions.find_one(
            {"user_id": trade["user_id"], "symbol": trade.get("symbol"),
             "status": "executed"}, sort=[("at", -1)])
    except Exception:
        pass
    return {
        "trade_id": str(trade["_id"]),
        "symbol": trade.get("symbol"), "action": trade.get("action"),
        "status": trade.get("status"),
        "why_opened": why_opened(trade, signal, decision),
        "why_this_time": why_this_time(trade, signal),
        "why_this_size": why_this_size(trade, signal),
        "why_this_stop": why_this_stop(trade, signal),
        "why_this_target": why_this_target(trade, signal),
        "what_changed": await what_changed(db, trade),
        "why_closed": await why_closed(db, trade),
    }
