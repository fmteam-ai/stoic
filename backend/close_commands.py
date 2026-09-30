"""Unified broker-position close protocol (audit r20 P2-01 / P2-02, r25 P2-01, r26 P2-01/P2-02).

EVERY writer that wants a broker position closed calls request_close(): it
allocates the trade's durable close_seq ATOMICALLY per trade (a single pipeline
findOneAndUpdate returns the post-increment document, so two concurrent callers
can never derive the same sequence), writes the denormalised current-command
pointer on the trade and an IMMUTABLE row per command in `close_commands`
(unique (trade_id, close_seq)). Callers that need the EA modification channel
pass `pending_modification`; it is stamped with the command-fence intent_id and
the per-trade command_seq in the SAME atomic update.
When the caller does not supply a session, the trade mutation + ledger insert +
supersede (and the acknowledgement's ledger + pointer update) run inside ONE
MongoDB transaction. Transactions are REQUIRED whenever the deployment can touch
capital (production, any live-like/terminal-bound account); the standalone
fallback exists only under the verified synthetic-only posture
(NL_EFFECTS_SYNTHETIC_ONLY=true with zero live-enabled accounts).
Pending-order cancellation is a different verb (cancel_pending) and never goes
through position close.
"""
import logging
import uuid
from datetime import datetime, timezone

from pymongo import ASCENDING, ReturnDocument

log = logging.getLogger("close_commands")


class TransactionsUnavailable(RuntimeError):
    """Raised when the deployment is capital-capable but MongoDB cannot run transactions."""


def _now():
    return datetime.now(timezone.utc).isoformat()


async def ensure_indexes(db):
    await db.close_commands.create_index([("trade_id", ASCENDING), ("close_seq", ASCENDING)],
                                         unique=True, name="uniq_trade_close_seq")


def _txn_unsupported(e: Exception) -> bool:
    s = str(e)
    return ("replica set" in s or "Transaction numbers" in s or "IllegalOperation" in s
            or getattr(e, "code", None) in (20, 263))


async def _with_txn(db, fn, *, emergency: bool = False):
    """Run fn(session) in a transaction. Without transaction support: fail closed
    when capital can be touched, else (synthetic-only posture) run plainly.
    `emergency` (PANIC — the risk-REDUCING brake) never refuses: it runs
    non-transactionally and records a loud `close_protocol_incident` so the
    deviation becomes a readiness incident instead of a silent fallback."""
    client = db.client
    try:
        async with await client.start_session() as s:
            # r26-b P2-02: the DRIVER's transaction retry contract — with_transaction re-runs the
            # callback on TransientTransactionError (write conflicts under close storms) and
            # retries ONLY the commit on UnknownTransactionCommitResult, so an ambiguous commit
            # can never allocate a second (trade_id, close_seq) for the same request.
            return await s.with_transaction(fn)
    except Exception as e:  # noqa: BLE001 — standalone mongod
        if not _txn_unsupported(e):
            raise
        from nl_execution import transactions_required
        if await transactions_required(db):
            if emergency:
                log.critical("close_commands: PANIC on a capital-capable deployment WITHOUT transactions — "
                             "executing the brake non-transactionally and raising an incident")
                await db.close_protocol_incidents.insert_one({
                    "kind": "panic_non_transactional", "at": _now(), "detail": str(e)[:200],
                    "resolution": "deploy a replica-set MongoDB (production boot already requires it)"})
                return await fn(None)
            raise TransactionsUnavailable(
                "close protocol requires a replica-set MongoDB (transactions) whenever capital can be "
                "touched — standalone fallback only under NL_EFFECTS_SYNTHETIC_ONLY=true with zero "
                "live-enabled accounts") from e
        log.warning("close_commands: transactions unavailable (standalone mongod) — synthetic-only posture, "
                    "running non-transactionally")
        return await fn(None)


def _lit(v):
    return {"$literal": v}


async def request_close(db, query: dict, *, reason: str, actor: str, ctx: dict | None = None,
                        session=None, stamp: dict | None = None,
                        pending_modification: dict | None = None, emergency: bool = False) -> dict:
    """Close every OPEN position matching `query` under one command id.
    Returns {command_id, trades_marked_for_close, trade_ids, commands}."""
    ctx = ctx or {}
    kw = dict(reason=reason, actor=actor, ctx=ctx, stamp=stamp, pending_modification=pending_modification)
    if session is not None:
        return await _request_close(db, query, session=session, **kw)
    return await _with_txn(db, lambda s: _request_close(db, query, session=s, **kw), emergency=emergency)


async def _request_close(db, query, *, reason, actor, ctx, session, stamp, pending_modification):
    now = _now()
    q = {**query, "status": "open"}
    trades = await db.trades.find(q, {"_id": 1}, session=session).to_list(length=1000)
    if not trades:
        return {"command_id": None, "trades_marked_for_close": 0, "trade_ids": [], "commands": []}
    command_id = ctx.get("idempotency_key") or f"close_{uuid.uuid4().hex}"
    command = {"key": command_id, "fence": ctx.get("fence"), "execution_id": ctx.get("execution_id"),
               "reason": reason, "actor": actor, "state": "requested", "requested_at": now}
    history_entry = {"key": command_id, "reason": reason, "actor": actor, "requested_at": now}
    set_stage = {
        "close_requested": _lit(True), "close_reason": _lit(reason), "close_idem_key": _lit(command_id),
        "close_fence": _lit(ctx.get("fence")), "close_command": _lit(command),
        "close_command_history": {"$slice": [
            {"$concatArrays": [{"$ifNull": ["$close_command_history", []]}, [_lit(history_entry)]]}, -20]},
    }
    for k, v in (stamp or {}).items():
        set_stage[k] = _lit(v)
    seq_stage = {"close_seq": {"$add": [{"$ifNull": ["$close_seq", 0]}, 1]}}
    if pending_modification is not None:
        # command-fence stamping (intent_id + monotonic command_seq) in the same atomic update
        mod = {**pending_modification, "intent_id": uuid.uuid4().hex, "close_command_key": command_id}
        mod.setdefault("requested_at", now)
        seq_stage["command_seq"] = {"$add": [{"$ifNull": ["$command_seq", 0]}, 1]}
        set_stage["pending_modification"] = {**{k: _lit(v) for k, v in mod.items()}, "seq": "$command_seq"}
    rows, ids, commands = [], [], []
    for t in trades:
        updated = await db.trades.find_one_and_update(
            {"_id": t["_id"], "status": "open"},
            [{"$set": seq_stage}, {"$set": set_stage}],
            projection={"_id": 1, "close_seq": 1, "close_command_history": 1, "symbol": 1,
                        "mt5_ticket": 1, "account_id": 1, "pending_modification": 1},
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
                     "pending_modification": updated.get("pending_modification") if pending_modification else None,
                     "state": "requested", "requested_at": now, "supersedes": prev_key,
                     "terminal_ack": None, "broker_result": None})
        commands.append({"trade_id": str(updated["_id"]), "close_seq": rows[-1]["close_seq"],
                         "pending_modification": updated.get("pending_modification")})
    if not rows:
        return {"command_id": None, "trades_marked_for_close": 0, "trade_ids": [], "commands": []}
    # a concurrent writer may already have allocated a HIGHER seq on the same trade before our
    # row exists (its supersede pass could not see us) — re-read and self-mark, so an older
    # command can never remain `requested` next to a newer one
    cur = {t["_id"]: t async for t in db.trades.find({"_id": {"$in": ids}}, {"close_seq": 1, "close_idem_key": 1},
                                                      session=session)}
    for r in rows:
        t = cur.get(next(i for i in ids if str(i) == r["trade_id"]), {})
        if int(t.get("close_seq") or 0) > r["close_seq"]:
            r.update({"state": "superseded", "superseded_by": t.get("close_idem_key"), "superseded_at": now})
    await db.close_commands.insert_many(rows, session=session)
    sup = [r for r in rows if r["supersedes"]]
    if sup:
        # exact (previous command, SAME trade) rows only — never a cross product
        # across trades, which could mark a newer command on another trade superseded
        await db.close_commands.update_many(
            {"_id": {"$in": [f"{r['supersedes']}:{r['trade_id']}" for r in sup]}, "state": "requested"},
            {"$set": {"state": "superseded", "superseded_by": command_id, "superseded_at": now}}, session=session)
    return {"command_id": command_id, "trades_marked_for_close": len(rows),
            "trade_ids": [str(i) for i in ids], "commands": commands}


async def acknowledge_close(db, trade: dict, *, broker_deal_id, occurred_at: str, result: str = "broker_confirmed",
                            session=None) -> dict | None:
    """Terminal/broker acknowledgement for the trade's outstanding command.
    Idempotent: only a `requested` ledger row for the trade's CURRENT close_seq is
    acknowledged; a repeated or stale (older seq) ack is a no-op. Ledger row and the
    trade's denormalised pointer commit together (r26 P2-02)."""
    cmd = trade.get("close_command") or {}
    if cmd.get("state") != "requested":
        return None
    kw = dict(broker_deal_id=broker_deal_id, occurred_at=occurred_at, result=result)
    if session is not None:
        return await _acknowledge_close(db, trade, session=session, **kw)
    return await _with_txn(db, lambda s: _acknowledge_close(db, trade, session=s, **kw))


async def _acknowledge_close(db, trade, *, broker_deal_id, occurred_at, result, session):
    cmd = trade["close_command"]
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
