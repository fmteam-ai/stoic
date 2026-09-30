"""Unified broker-position close protocol (audit r20 P2-01 / P2-02, r25 P2-01).

EVERY writer that wants a broker position closed calls request_close(): it
allocates the trade's durable close_seq ATOMICALLY per trade (findOneAndUpdate
returns the post-increment document, so two concurrent callers can never derive
the same sequence), writes the denormalised current-command pointer on the trade
and an IMMUTABLE row per command in `close_commands` (unique (trade_id, close_seq)).
When the caller does not supply a session, the trade mutation + ledger insert +
supersede run inside ONE MongoDB transaction (replica set — required in
production); on a standalone dev Mongo the same steps run without a transaction.
Pending-order cancellation is a different verb (cancel_pending) and never goes
through position close.
"""
import logging
import uuid
from datetime import datetime, timezone

from pymongo import ASCENDING, ReturnDocument

log = logging.getLogger("close_commands")


def _now():
    return datetime.now(timezone.utc).isoformat()


async def ensure_indexes(db):
    await db.close_commands.create_index([("trade_id", ASCENDING), ("close_seq", ASCENDING)],
                                         unique=True, name="uniq_trade_close_seq")


async def _with_txn(db, fn):
    """Run fn(session) in a transaction when the server supports it, else plainly."""
    client = db.client
    try:
        async with await client.start_session() as s:
            async with s.start_transaction():
                return await fn(s)
    except Exception as e:  # noqa: BLE001 — standalone mongod: "Transaction numbers are only allowed on a replica set member"
        if "replica set" in str(e) or "Transaction numbers" in str(e) or "IllegalOperation" in str(e):
            log.warning("close_commands: transactions unavailable (standalone mongod) — running non-transactionally")
            return await fn(None)
        raise


async def request_close(db, query: dict, *, reason: str, actor: str, ctx: dict | None = None,
                        session=None, stamp: dict | None = None) -> dict:
    """Close every OPEN position matching `query` under one command id.
    Returns {command_id, trades_marked_for_close, trade_ids}."""
    ctx = ctx or {}
    if session is not None:
        return await _request_close(db, query, reason=reason, actor=actor, ctx=ctx, session=session, stamp=stamp)
    return await _with_txn(db, lambda s: _request_close(db, query, reason=reason, actor=actor, ctx=ctx,
                                                        session=s, stamp=stamp))


async def _request_close(db, query, *, reason, actor, ctx, session, stamp):
    now = _now()
    q = {**query, "status": "open"}
    trades = await db.trades.find(q, {"_id": 1}, session=session).to_list(length=1000)
    if not trades:
        return {"command_id": None, "trades_marked_for_close": 0, "trade_ids": []}
    command_id = ctx.get("idempotency_key") or f"close_{uuid.uuid4().hex}"
    command = {"key": command_id, "fence": ctx.get("fence"), "execution_id": ctx.get("execution_id"),
               "reason": reason, "actor": actor, "state": "requested", "requested_at": now}
    rows, ids = [], []
    for t in trades:
        # atomic per-trade sequence: the returned doc carries the NEW close_seq and the PREVIOUS
        # command (captured in the same update via history), so no two callers share a seq
        updated = await db.trades.find_one_and_update(
            {"_id": t["_id"], "status": "open"},
            {"$inc": {"close_seq": 1},
             "$set": {"close_requested": True, "close_reason": reason, "close_idem_key": command_id,
                      "close_fence": ctx.get("fence"), "close_command": command, **(stamp or {})},
             "$push": {"close_command_history": {"$each": [{"key": command_id, "reason": reason, "actor": actor,
                                                            "requested_at": now}], "$slice": -20}}},
            projection={"_id": 1, "close_seq": 1, "close_command_history": 1, "symbol": 1,
                        "mt5_ticket": 1, "account_id": 1},
            return_document=ReturnDocument.AFTER, session=session)
        if not updated:
            continue   # closed/raced away between find and update — nothing to command
        hist = updated.get("close_command_history") or []
        prev_key = hist[-2]["key"] if len(hist) >= 2 else None
        ids.append(updated["_id"])
        rows.append({"_id": f"{command_id}:{updated['_id']}", "command_id": command_id,
                     "trade_id": str(updated["_id"]), "account_id": updated.get("account_id"),
                     "symbol": updated.get("symbol"), "mt5_ticket": updated.get("mt5_ticket"),
                     "close_seq": int(updated.get("close_seq") or 0), "fence": ctx.get("fence"),
                     "execution_id": ctx.get("execution_id"), "decision_id": ctx.get("decision_id"),
                     "authority_version": ctx.get("authority_version"), "reason": reason, "actor": actor,
                     "state": "requested", "requested_at": now, "supersedes": prev_key,
                     "terminal_ack": None, "broker_result": None})
    if not rows:
        return {"command_id": None, "trades_marked_for_close": 0, "trade_ids": []}
    await db.close_commands.insert_many(rows, session=session)
    sup = [r for r in rows if r["supersedes"]]
    if sup:
        await db.close_commands.update_many(
            {"command_id": {"$in": [r["supersedes"] for r in sup]},
             "trade_id": {"$in": [r["trade_id"] for r in sup]}, "state": "requested"},
            {"$set": {"state": "superseded", "superseded_by": command_id, "superseded_at": now}}, session=session)
    return {"command_id": command_id, "trades_marked_for_close": len(rows),
            "trade_ids": [str(i) for i in ids]}


async def acknowledge_close(db, trade: dict, *, broker_deal_id, occurred_at: str, result: str = "broker_confirmed",
                            session=None) -> dict | None:
    """Terminal/broker acknowledgement for the trade's outstanding command.
    Idempotent: only a `requested` ledger row for the trade's CURRENT close_seq is
    acknowledged; a repeated or stale (older seq) ack is a no-op, and the trade's
    denormalised pointer is updated in the same operation set."""
    cmd = trade.get("close_command") or {}
    if cmd.get("state") != "requested":
        return None
    seq = trade.get("close_seq")
    res = await db.close_commands.update_one(
        {"_id": f"{cmd.get('key')}:{trade['_id']}", "trade_id": str(trade["_id"]), "close_seq": seq,
         "state": "requested"},
        {"$set": {"state": result, "broker_result": {"deal_id": broker_deal_id, "at": occurred_at},
                  "terminal_ack": {"at": occurred_at}}}, session=session)
    if res.matched_count == 0:
        return None   # already acknowledged, superseded, or not this sequence — idempotent no-op
    ack = {**cmd, "state": result, "close_seq": seq, "broker_deal_id": broker_deal_id, "confirmed_at": occurred_at}
    await db.trades.update_one({"_id": trade["_id"], "close_command.key": cmd.get("key")},
                               {"$set": {"close_command": ack}}, session=session)
    return ack


async def cancel_pending(db, query: dict, *, reason: str, actor: str, session=None, stamp: dict | None = None) -> int:
    """Pending ORDERS are cancelled, never position-closed."""
    res = await db.trades.update_many(
        {**query, "status": "pending"},
        {"$set": {"status": "cancelled", "close_reason": reason, "cancelled_by": actor,
                  "closed_at": _now(), **(stamp or {})}}, session=session)
    return res.modified_count
