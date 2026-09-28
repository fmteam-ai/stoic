"""Unified broker-position close protocol (audit r20 P2-01 / P2-02).

EVERY writer that wants a broker position closed calls request_close(): it
allocates the trade's durable close_seq atomically with the command, writes the
denormalised current-command pointer on the trade and an IMMUTABLE row per
command in `close_commands` (sequence, idempotency key, execution/proposal ids,
target, requester, reason, timestamps, state, terminal/broker ack, superseded_by).
Pending-order cancellation is a different verb (cancel_pending) and never goes
through position close.
"""
import uuid
from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc).isoformat()


async def request_close(db, query: dict, *, reason: str, actor: str, ctx: dict | None = None,
                        session=None, stamp: dict | None = None) -> dict:
    """Close every OPEN position matching `query` under one command id.
    Returns {command_id, trades_marked_for_close, trade_ids}."""
    ctx = ctx or {}
    now = _now()
    q = {**query, "status": "open"}
    trades = await db.trades.find(q, {"_id": 1, "close_seq": 1, "close_command": 1, "symbol": 1,
                                      "mt5_ticket": 1, "account_id": 1}, session=session).to_list(length=1000)
    if not trades:
        return {"command_id": None, "trades_marked_for_close": 0, "trade_ids": []}
    command_id = ctx.get("idempotency_key") or f"close_{uuid.uuid4().hex}"
    ids = [t["_id"] for t in trades]
    res = await db.trades.update_many(
        {"_id": {"$in": ids}, "status": "open"},
        {"$inc": {"close_seq": 1},
         "$set": {"close_requested": True, "close_reason": reason, "close_idem_key": command_id,
                  "close_fence": ctx.get("fence"),
                  "close_command": {"key": command_id, "fence": ctx.get("fence"),
                                    "execution_id": ctx.get("execution_id"), "reason": reason, "actor": actor,
                                    "state": "requested", "requested_at": now},
                  **(stamp or {})},
         "$push": {"close_command_history": {"$each": [{"key": command_id, "reason": reason, "actor": actor,
                                                        "requested_at": now}], "$slice": -20}}},
        session=session)
    rows = []
    for t in trades:
        prev = t.get("close_command") or {}
        rows.append({"_id": f"{command_id}:{t['_id']}", "command_id": command_id, "trade_id": str(t["_id"]),
                     "account_id": t.get("account_id"), "symbol": t.get("symbol"), "mt5_ticket": t.get("mt5_ticket"),
                     "close_seq": int(t.get("close_seq") or 0) + 1, "fence": ctx.get("fence"),
                     "execution_id": ctx.get("execution_id"), "decision_id": ctx.get("decision_id"),
                     "authority_version": ctx.get("authority_version"), "reason": reason, "actor": actor,
                     "state": "requested", "requested_at": now, "supersedes": prev.get("key"),
                     "terminal_ack": None, "broker_result": None})
    await db.close_commands.insert_many(rows, session=session)
    if any(r["supersedes"] for r in rows):
        await db.close_commands.update_many(
            {"command_id": {"$in": [r["supersedes"] for r in rows if r["supersedes"]]},
             "trade_id": {"$in": [r["trade_id"] for r in rows]}, "state": "requested"},
            {"$set": {"state": "superseded", "superseded_by": command_id, "superseded_at": now}}, session=session)
    return {"command_id": command_id, "trades_marked_for_close": res.modified_count,
            "trade_ids": [str(i) for i in ids]}


async def acknowledge_close(db, trade: dict, *, broker_deal_id, occurred_at: str, result: str = "broker_confirmed") -> dict | None:
    """Terminal/broker acknowledgement for the trade's outstanding command."""
    cmd = trade.get("close_command") or {}
    if cmd.get("state") != "requested":
        return None
    ack = {**cmd, "state": result, "close_seq": trade.get("close_seq"), "broker_deal_id": broker_deal_id,
           "confirmed_at": occurred_at}
    await db.close_commands.update_one(
        {"_id": f"{cmd.get('key')}:{trade['_id']}"},
        {"$set": {"state": result, "broker_result": {"deal_id": broker_deal_id, "at": occurred_at},
                  "terminal_ack": {"at": occurred_at}}})
    return ack


async def cancel_pending(db, query: dict, *, reason: str, actor: str, session=None, stamp: dict | None = None) -> int:
    """Pending ORDERS are cancelled, never position-closed."""
    res = await db.trades.update_many(
        {**query, "status": "pending"},
        {"$set": {"status": "cancelled", "close_reason": reason, "cancelled_by": actor,
                  "closed_at": _now(), **(stamp or {})}}, session=session)
    return res.modified_count
