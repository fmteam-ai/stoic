"""Admin-only panic-lock endpoint + per-user kill switch.

Two surfaces:
  - POST /api/admin/panic        : global. Closes all pending+open trades,
                                   disables every bot_config. Admin role only.
  - POST /api/panic              : per-user. Same blast radius scoped to the
                                   calling user. Any authenticated user can use it.
"""
import uuid
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Request

from auth import get_current_user
import nl_execution as nx
from database import get_db
from step_up import audit_event
from ws_manager import manager as ws_manager
from rate_limiter import reset as reset_rate_limiter

router = APIRouter(tags=["panic"])


def halt_local_scalp_runners(user_id: str | None) -> int:
    """Flip every in-process scalp runner (of `user_id`, or all) to disabled."""
    from scalp.engine import _runners
    n = 0
    for r in list(_runners.values()):
        if user_id is None or r.user_id == user_id:
            if r.enabled:
                n += 1
            r.enabled = False
    return n


async def _disable_all_bots_and_close_trades(query: dict, broadcast_user_id: str = None,
                                             stamp: dict | None = None, session=None) -> dict:
    """`stamp` = nl_execution.effect_stamp(ctx) when invoked from the fenced NL executor."""
    db = get_db()
    stamp = stamp or {}
    now_iso = datetime.now(timezone.utc).isoformat()
    bot_result = await db.bot_configs.update_many(
        query,
        {"$set": {
            "active": False,
            "tripped_at": now_iso,
            "tripped_reason": "PANIC LOCK — all trading halted by user/admin", **stamp,
        }}, session=session,
    )
    # Fix plan A2/B2 — PANIC also stops the scalp fast path (persisted config +
    # in-process runners) and LOCKS the account(s): the authority choke point,
    # the tick ingress (re-reads the account every batch, so it works across
    # workers) and the poll dispatch fence all refuse new exposure until the
    # lock is released via /bot/start (panic_release step-up).
    scalp_result = await db.scalp_configs.update_many(
        {**query, "enabled": True},
        {"$set": {"enabled": False, "panic_disabled_at": now_iso, **stamp}}, session=session,
    )
    # A4 — scope decides who may release: a user's own PANIC (`user`) is released by
    # that user via /bot/start; an admin-wide PANIC (`platform`) only via /admin/panic/release.
    lock = {"reason": "panic", "at": now_iso, "by": broadcast_user_id or "admin",
            "scope": "user" if query.get("user_id") else "platform"}
    # A5 — a user's PANIC never rewrites an admin-wide (platform) lock as their own
    lock_q = dict(query)
    if lock["scope"] == "user":
        lock_q["$nor"] = [PLATFORM_LOCK_MATCH]
    acct_result = await db.accounts.update_many(
        lock_q, {"$set": {"trading_authority": "LOCKED", "authority_lock": lock}}, session=session,
    )
    halt_local_scalp_runners(query.get("user_id"))
    # Mark all pending trades cancelled
    trade_cancel = await db.trades.update_many(
        {**query, "status": "pending"},
        {"$set": {"status": "cancelled", "error": "panic_lock",
                  "close_reason": "panic",
                  "closed_at": now_iso, **stamp}}, session=session,
    )
    # Request close on all open trades through the unified close protocol (r20 P2-01)
    from close_commands import request_close
    nl = stamp.get("nl_effect") or {}
    closed = await request_close(db, query, reason="panic", actor=f"panic:{broadcast_user_id or 'admin'}",
                                 ctx={"idempotency_key": nl.get("key"), "fence": nl.get("fence"),
                                      "execution_id": nl.get("execution_id"), "decision_id": nl.get("decision_id"),
                                      "authority_version": nl.get("authority_version")},
                                 session=session, stamp=stamp, emergency=True)   # r26 P2-02: the brake never refuses
    payload = {
        "bots_disabled": bot_result.modified_count,
        "scalp_runners_disabled": scalp_result.modified_count,
        "accounts_locked": acct_result.modified_count,
        "trades_cancelled": trade_cancel.modified_count,
        "open_trades_marked_for_close": closed["trades_marked_for_close"],
        "close_command_id": closed["command_id"],
        "at": now_iso,
    }
    # r20 P2-03: the authority-version event is part of the SAME transaction
    from canonical_decision import bump_authority_version
    payload["authority_version"] = await bump_authority_version(db, "panic", user_id=broadcast_user_id, session=session)
    # r18 P1-02: the transaction stays DATABASE-ONLY. The websocket broadcast and the
    # authority-version bump are written to an outbox row in the same session and
    # published only after commit (publish_panic_outbox).
    outbox = {"_id": uuid.uuid4().hex, "kind": "panic_lock", "user_id": broadcast_user_id,
              "payload": payload, "state": "pending", "created_at": now_iso}
    await db.ops_outbox.insert_one(outbox, session=session)
    payload["outbox_id"] = outbox["_id"]
    if session is None:
        await publish_panic_outbox(db, outbox["_id"])
    return payload


async def publish_panic_outbox(db, outbox_id: str | None) -> bool:
    """Post-commit websocket notification, exactly once per outbox row: leased
    claim (pending → publishing with lease_until + attempts) so a crash after the
    claim is RECOVERED by sweep_ops_outbox(); outcome delivered/failed/unknown
    is recorded (r20 P2-03). The authority bump already committed in the txn."""
    if not outbox_id:
        return False
    now = datetime.now(timezone.utc)
    row = await db.ops_outbox.find_one_and_update(
        {"_id": outbox_id, "$or": [{"state": "pending"},
                                   {"state": "publishing", "lease_until": {"$lt": now.isoformat()}}]},
        {"$set": {"state": "publishing", "publishing_at": now.isoformat(),
                  "lease_until": (now + timedelta(seconds=OUTBOX_LEASE_S)).isoformat()},
         "$inc": {"attempts": 1}})
    if not row:
        return False
    attempt = (row.get("attempts") or 0) + 1
    if attempt > OUTBOX_MAX_ATTEMPTS:
        # r25 P2-02: terminal state only after the retry budget is spent — an
        # explicit incident, never a silent "published".
        await db.ops_outbox.update_one({"_id": outbox_id}, {"$set": {
            "state": "failed", "outcome": f"delivery failed after {OUTBOX_MAX_ATTEMPTS} attempts",
            "ended_at": now.isoformat()}})
        return False
    try:
        if row.get("user_id"):
            await ws_manager.broadcast(row["user_id"], "panic_lock", row["payload"])
    except Exception as e:  # noqa: BLE001
        # r25 P2-02: a transient delivery error is RETRYABLE — back to pending with
        # exponential backoff (the sweeper picks it up once `not_before` passes).
        backoff = min(OUTBOX_LEASE_S * (2 ** (attempt - 1)), 600)
        await db.ops_outbox.update_one({"_id": outbox_id, "state": "publishing"}, {"$set": {
            "state": "pending", "last_error": f"{type(e).__name__}: {e}"[:200],
            "not_before": (datetime.now(timezone.utc) + timedelta(seconds=backoff)).isoformat()},
            "$unset": {"lease_until": ""}})
        return False
    await db.ops_outbox.update_one({"_id": outbox_id, "state": "publishing"}, {"$set": {
        "state": "published", "outcome": "delivered", "published_at": datetime.now(timezone.utc).isoformat()}})
    return True


@router.post("/panic")
async def panic_user(request: Request, user=Depends(get_current_user)):
    """Per-user panic. Halts bot + cancels pending + requests close on opens."""
    reset_rate_limiter(user["id"])
    result = await _disable_all_bots_and_close_trades(
        {"user_id": user["id"]}, broadcast_user_id=user["id"]
    )
    await audit_event(get_db(), user["id"], "panic_triggered", result, request)
    return result


@router.post("/admin/panic")
async def panic_global(request: Request, user=Depends(get_current_user)):
    """Global panic — every user's bot down. Admin role only."""
    from auth import require_admin
    require_admin(user)
    reset_rate_limiter()  # clear all rate-limit buckets
    result = await _disable_all_bots_and_close_trades({})
    await audit_event(get_db(), user["id"], "panic_triggered_global", result, request)
    return result


# A platform (admin-wide) lock: explicit scope, or a legacy lock without scope stamped by "admin".
PLATFORM_LOCK_MATCH = {"authority_lock.reason": "panic",
                       "$or": [{"authority_lock.scope": "platform"},
                               {"authority_lock.scope": {"$exists": False}, "authority_lock.by": "admin"}]}
USER_LOCK_MATCH = {"authority_lock.reason": "panic", "$nor": [PLATFORM_LOCK_MATCH]}


async def release_panic_locks(db, query: dict, *, actor: str, via: str) -> int:
    """Unset PANIC locks matching `query`; stamps who released them."""
    res = await db.accounts.update_many(
        {**query, "authority_lock.reason": "panic"},
        {"$unset": {"trading_authority": "", "authority_lock": ""},
         "$set": {"authority_lock_released": {
             "at": datetime.now(timezone.utc).isoformat(), "by": actor, "via": via}}})
    if res.modified_count:
        from canonical_decision import bump_authority_version
        await bump_authority_version(db, f"panic_release:{via}", user_id=actor)
    return res.modified_count


@router.post("/admin/panic/release")
async def panic_release_global(request: Request, user=Depends(get_current_user)):
    """A4 — release admin-wide (and any remaining) PANIC locks. Admin + step-up."""
    from auth import require_admin
    from step_up import require_step_up
    require_admin(user)
    db = get_db()
    await require_step_up(db, user, request, "panic_release")
    # G6 — releases the ADMIN-WIDE lock only; a user's own PANIC stays theirs to release
    n = await release_panic_locks(db, PLATFORM_LOCK_MATCH, actor=user["id"], via="admin_panic_release")
    await audit_event(db, user["id"], "panic_release_global", {"accounts_unlocked": n}, request, step_up=True)
    return {"accounts_unlocked": n}


OUTBOX_LEASE_S = 30
OUTBOX_MAX_ATTEMPTS = 5


async def sweep_ops_outbox(db) -> int:
    """Reclaim pending rows and publishing rows whose lease expired (crash recovery)."""
    now = datetime.now(timezone.utc).isoformat()
    n = 0
    async for row in db.ops_outbox.find({"kind": "panic_lock", "$or": [
            {"state": "pending", "$or": [{"not_before": {"$exists": False}}, {"not_before": {"$lte": now}}]},
            {"state": "publishing", "lease_until": {"$lt": now}}]}, {"_id": 1}):
        if await publish_panic_outbox(db, row["_id"]):
            n += 1
    return n


async def ops_outbox_health(db) -> dict:
    """r25 P2-02: pending/failed/unknown counts for readiness and alerts."""
    out = {}
    for st in ("pending", "publishing", "failed", "unknown"):
        out[st] = await db.ops_outbox.count_documents({"kind": "panic_lock", "state": st})
    out["ok"] = out["failed"] == 0 and out["unknown"] == 0
    return out


async def ops_outbox_loop():
    """Background sweeper (in-process workers): recovers stuck/expired outbox rows."""
    import asyncio
    while True:
        try:
            await asyncio.sleep(20)
            await sweep_ops_outbox(get_db())
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            import logging
            logging.getLogger("ops_outbox").warning("sweep error: %s", e)
