"""iter-160 — order lifecycle audit: stitches every stage of a trade
(signal → validation → risk → execution → confirmation → monitoring →
close → analytics) into one auditable timeline from data that is already
recorded (trades doc + trade_events stream)."""
from datetime import datetime


def _iso(v):
    if isinstance(v, datetime):
        return v.isoformat()
    return v


async def assemble_timeline(db, trade_id: str) -> dict | None:
    from bson import ObjectId
    from bson.errors import InvalidId
    q = []
    try:
        q.append({"_id": ObjectId(trade_id)})
    except (InvalidId, TypeError):
        pass
    q.extend([{"trade_id": trade_id}, {"mt5_ticket": trade_id}])
    trade = None
    for cond in q:
        trade = await db.trades.find_one(cond)
        if trade:
            break
    if not trade:
        return None
    tid = str(trade["_id"])
    stages = []

    def stage(name, at, summary, detail=None):
        stages.append({"stage": name, "at": _iso(at),
                       "status": "complete" if at else "pending",
                       "summary": str(summary)[:300],
                       "detail": detail or {}})

    snap = trade.get("explanation_snapshot") or {}
    stage("signal", trade.get("opened_at"),
          f"{trade.get('action', '?').upper()} {trade.get('base_symbol')} — "
          f"origin {trade.get('origin', 'auto')}",
          {"explanation": snap, "market_regime": trade.get("market_regime")})

    ident = trade.get("broker_identity") or {}
    stage("validation", trade.get("opened_at"),
          ("verified broker identity "
           f"{ident.get('account_number', 'n/a')}@{ident.get('server', 'n/a')}"
           if ident else "identity snapshot unavailable (legacy trade)"),
          {"broker_identity": ident, "mode": trade.get("mode"),
           "is_test": trade.get("is_test", False)})

    stage("risk", trade.get("opened_at"),
          f"risk {trade.get('risk_pct', '?')}% → lot "
          f"{trade.get('lot_size')} (sl {trade.get('original_stop_loss')})",
          {"safety_audit": trade.get("safety_audit"),
           "original_lot_size": trade.get("original_lot_size")})

    stage("execution", trade.get("opened_at"),
          f"entry {trade.get('entry_price')} · MT5 ticket "
          f"{trade.get('mt5_ticket') or 'pending'}",
          {"error": trade.get("error")})

    events = [e async for e in db.trade_events.find(
        {"trade_id": {"$in": [tid, trade.get("mt5_ticket")]}},
        {"_id": 0}).sort("at", 1).limit(100)]
    fills = [e for e in events if "fill" in str(e.get("event_type", ""))]
    stage("confirmation",
          fills[0].get("at") if fills else trade.get("opened_at"),
          (f"{len(fills)} fill event(s), "
           f"{sum(float(e.get('filled_lots') or 0) for e in fills):g} lots"
           if fills else "no broker fill events recorded"),
          {"events": fills[:10]})

    monitor_bits = []
    if trade.get("breakeven_set"):
        monitor_bits.append("breakeven applied")
    if trade.get("partial_closed"):
        monitor_bits.append("partial close taken")
    others = [e for e in events if "fill" not in str(e.get("event_type", ""))]
    stage("monitoring", trade.get("opened_at"),
          ", ".join(monitor_bits) or "no in-trade adjustments",
          {"events": others[:10]})

    if trade.get("closed_at"):
        stage("close", trade.get("closed_at"),
              f"exit {trade.get('exit_price')} · pnl {trade.get('pnl')}",
              {})
        stage("analytics", trade.get("closed_at"),
              f"pnl {trade.get('pnl')} · regime "
              f"{trade.get('market_regime', 'n/a')}",
              {"account_id": trade.get("account_id")})
    else:
        stage("close", None, "position still open", {})

    return {"trade_id": tid,
            "user_id": str(trade.get("user_id", "")),
            "symbol": trade.get("base_symbol"),
            "status": "closed" if trade.get("closed_at") else "open",
            "stages": stages,
            "complete": sum(1 for s in stages if s["status"] == "complete")}
