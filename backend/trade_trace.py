"""Phase G · Trace observability — ONE trace id follows every trade through

    Tick → Features → Model → Risk → OMS → Broker → Reconciliation → Analytics

The scalp `decision_id` IS the trace id (stamped on the decision ledger,
risk reservations, the trade document, every trade_event and the financial
ledger). `assemble_trace` reconstructs the full story from those durable
projections and answers "why did this trade happen?" in a single payload:
stages, a merged chronological timeline, and a deterministic narrative.
"""
from datetime import datetime

STAGE_ORDER = ("tick", "features", "model", "risk", "oms", "broker",
               "reconciliation", "analytics")


def _ms(v) -> int | None:
    """Normalise datetimes / iso strings / ms ints to epoch ms."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, datetime):
        return int(v.timestamp() * 1000)
    try:
        return int(datetime.fromisoformat(str(v).replace("Z", "+00:00"))
                   .timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def build_stages(decision: dict, trade: dict | None,
                 reservations: list, financial_events: list,
                 daily_stats: dict | None) -> dict:
    """Deterministic stage summaries from the durable projections."""
    d = decision or {}
    feats = d.get("features") or {}
    fc = d.get("forecast") or {}
    dq = d.get("decision_quality") or {}
    stages = {
        "tick": {
            "ts_ms": d.get("signal_ts_ms") or d.get("ts_ms"),
            "spread_pips": feats.get("spread_pips"),
            "mid": feats.get("mid"),
            "vol_short_pips": feats.get("vol_short"),
        },
        "features": {k: feats.get(k) for k in
                     ("mom_pips", "vol_short", "vol_long", "spread_pctl",
                      "tick_rate", "imbalance") if k in feats},
        "model": {
            "p_target_before_stop": fc.get("p_target_before_stop"),
            "model_source": d.get("model_source"),
            "setup_preset": d.get("setup_preset"),
            "direction": d.get("direction"),
            "ev": d.get("ev"),
            "net_edge_pips": d.get("net_edge_pips"),
            "decision_quality": {k: dq.get(k) for k in
                                 ("score", "verdict", "pillars", "weakest")
                                 if k in dq},
        },
        "risk": {
            "verdict": d.get("verdict"),
            "reject_stage": d.get("reject_stage"),
            "lot": (d.get("risk") or {}).get("lot"),
            "vol_size_mult": (d.get("risk") or {}).get("vol_size_mult"),
            "portfolio": d.get("portfolio"),
            "reservations": [
                {"state": r.get("state"), "risk_usd": r.get("risk_usd"),
                 "release_reason": r.get("release_reason"),
                 "transitions": [t.get("state")
                                 for t in (r.get("transitions") or [])]}
                for r in reservations],
        },
        "oms": {
            "trade_id": str(trade["_id"]) if trade else None,
            "lifecycle_state": (trade or {}).get("lifecycle_state"),
            "lifecycle": [{"state": e.get("state"), "at_ms": _ms(e.get("at"))}
                          for e in (trade or {}).get("lifecycle") or []],
            "adaptive_actions": (trade or {}).get("adaptive_actions"),
        },
        "broker": {
            "mt5_ticket": (trade or {}).get("mt5_ticket"),
            "entry_price": (trade or {}).get("entry_price"),
            "stop_loss": (trade or {}).get("stop_loss"),
            "take_profit": (trade or {}).get("take_profit"),
            "status": (trade or {}).get("status"),
            "close_reason": (trade or {}).get("close_reason"),
            "profit": (trade or {}).get("profit"),
        },
        "reconciliation": {
            "financial_events": [
                {"deal_id": f.get("deal_id"), "event_type": f.get("event_type"),
                 "net_pnl_usd": f.get("net_pnl_usd"),
                 "status": f.get("status")} for f in financial_events],
            "outcome": d.get("outcome"),
        },
        "analytics": daily_stats and {
            "day": daily_stats.get("day"),
            "win_rate": daily_stats.get("win_rate"),
            "net_pips": daily_stats.get("net_pips"),
            "decisions": daily_stats.get("decisions"),
        },
    }
    return stages


def build_timeline(decision: dict, trade: dict | None, events: list,
                   reservations: list, financial_events: list) -> list:
    """Every recorded moment, merged and time-ordered."""
    tl: list = []
    d = decision or {}
    if d.get("ts_ms"):
        tl.append({"ts_ms": d["ts_ms"], "stage": "model",
                   "what": f"decision {d.get('verdict') or 'evaluated'}"
                   + (f" ({d.get('reject_stage')})"
                      if d.get("reject_stage") else "")})
    for r in reservations:
        for t in r.get("transitions") or []:
            ts = _ms(t.get("at"))
            if ts:
                tl.append({"ts_ms": ts, "stage": "risk",
                           "what": f"reservation {t.get('state')}"})
    for e in (trade or {}).get("lifecycle") or []:
        ts = _ms(e.get("at"))
        if ts:
            tl.append({"ts_ms": ts, "stage": "oms",
                       "what": f"order {e.get('state')}"})
    for a in (trade or {}).get("adaptive_actions") or []:
        if a.get("ts_ms"):
            tl.append({"ts_ms": a["ts_ms"], "stage": "oms",
                       "what": f"adaptive {a.get('action')}"
                               f" ({a.get('reason')})"})
    for ev in events:
        if ev.get("ts_ms"):
            tl.append({"ts_ms": ev["ts_ms"], "stage": "broker",
                       "what": ev.get("event_type"),
                       "payload": ev.get("payload")})
    for f in financial_events:
        ts = _ms(f.get("created_at"))
        if ts:
            tl.append({"ts_ms": ts, "stage": "reconciliation",
                       "what": f"deal {f.get('deal_id')} "
                               f"{f.get('event_type')} "
                               f"(${f.get('net_pnl_usd')})"})
    tl.sort(key=lambda x: x["ts_ms"])
    return tl


def build_narrative(decision: dict, trade: dict | None) -> list:
    """The human answer to 'why did this trade happen?' — deterministic
    sentences built only from recorded data."""
    d = decision or {}
    feats = d.get("features") or {}
    fc = d.get("forecast") or {}
    dq = d.get("decision_quality") or {}
    out = []
    out.append(f"{d.get('symbol')} {d.get('direction')} setup "
               f"'{(d.get('setup') or {}).get('kind', 'pullback')}' detected"
               + (f" using preset {d.get('setup_preset')}"
                  if d.get("setup_preset") else "")
               + (f" with spread {round(float(feats['spread_pips']), 2)} pips"
                  if feats.get("spread_pips") is not None else "") + ".")
    if fc.get("p_target_before_stop") is not None:
        out.append(f"Model ({d.get('model_source') or 'model'}) put the "
                   f"chance of hitting target before stop at "
                   f"{round(float(fc['p_target_before_stop']) * 100)}%.")
    if d.get("net_edge_pips") is not None:
        out.append(f"Net edge after costs: {d['net_edge_pips']} pips"
                   + (f" (EV ${(d.get('ev') or {}).get('ev_usd')})"
                      if (d.get("ev") or {}).get("ev_usd") is not None
                      else "") + ".")
    if dq.get("score") is not None:
        out.append(f"Combined decision quality {dq['score']}/100 "
                   f"({dq.get('verdict')}); weakest pillars: "
                   f"{', '.join(dq.get('weakest') or [])}.")
    if d.get("verdict") == "rejected":
        out.append(f"REJECTED at stage '{d.get('reject_stage')}' — "
                   f"no order was sent.")
        return out
    if (d.get("risk") or {}).get("lot"):
        out.append(f"Risk approved {(d['risk'] or {}).get('lot')} lots"
                   + (f" (volatility downscale ×"
                      f"{(d.get('risk') or {}).get('vol_size_mult')})"
                      if (d.get("risk") or {}).get("vol_size_mult") else "")
                   + ".")
    if trade:
        if trade.get("entry_price"):
            out.append(f"Broker filled at {trade['entry_price']} "
                       f"(stop {trade.get('stop_loss')}, "
                       f"target {trade.get('take_profit')}).")
        if trade.get("status") == "closed":
            out.append(f"Closed ({trade.get('close_reason') or 'target/stop'})"
                       f" with P&L ${trade.get('profit')}.")
        elif trade.get("lifecycle_state"):
            out.append(f"Current state: {trade['lifecycle_state']}.")
    return out


async def assemble_trace(db, trace_id: str) -> dict | None:
    """trace_id = decision_id (preferred) or trade_id. Returns the single
    view: stages + timeline + narrative, or None when unknown."""
    decision = await db.scalp_decisions.find_one({"decision_id": trace_id},
                                                 {"_id": 0})
    trade = None
    if decision is None:
        from bson import ObjectId
        try:
            trade = await db.trades.find_one({"_id": ObjectId(trace_id)})
        except Exception:
            trade = None
        if trade is None:
            return None
        if trade.get("scalp_decision_id"):
            decision = await db.scalp_decisions.find_one(
                {"decision_id": trade["scalp_decision_id"]}, {"_id": 0})
    did = (decision or {}).get("decision_id") or trace_id
    if trade is None:
        trade = await db.trades.find_one({"scalp_decision_id": did})
    tid = str(trade["_id"]) if trade else None

    reservations = [r async for r in
                    db.risk_reservations.find({"decision_id": did}, {"_id": 0})]
    ev_q = {"$or": [{"decision_id": did}]}
    if tid:
        ev_q["$or"].append({"trade_id": tid})
    events = [e async for e in db.trade_events.find(ev_q, {"_id": 0})
              .sort("ts_ms", 1).limit(200)]
    fins = ([f async for f in
             db.scalp_financial_events.find({"trade_id": tid}, {"_id": 0})]
            if tid else [])
    daily = None
    if decision:
        from datetime import datetime, timezone
        day = datetime.fromtimestamp((decision.get("ts_ms") or 0) / 1000,
                                     tz=timezone.utc).strftime("%Y-%m-%d")
        daily = await db.scalp_daily_stats.find_one(
            {"_id": f"{day}:{decision.get('symbol')}:"
                    f"{decision.get('model_key')}"}, {"_id": 0})

    return {
        "trace_id": did,
        "user_id": (decision or {}).get("user_id") or
                   (trade or {}).get("user_id"),
        "symbol": (decision or {}).get("symbol") or
                  (trade or {}).get("symbol"),
        "stages": build_stages(decision or {}, trade, reservations,
                               fins, daily),
        "timeline": build_timeline(decision or {}, trade, events,
                                   reservations, fins),
        "narrative": build_narrative(decision or {}, trade),
        "event_count": len(events),
    }
