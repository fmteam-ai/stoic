"""Admin-only panic-lock endpoint + per-user kill switch.

Two surfaces:
  - POST /api/admin/panic        : global. Closes all pending+open trades,
                                   disables every bot_config. Admin role only.
  - POST /api/panic              : per-user. Same blast radius scoped to the
                                   calling user. Any authenticated user can use it.
"""
import uuid
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Request

from auth import get_current_user
import nl_execution as nx
from database import get_db
from step_up import audit_event
from ws_manager import manager as ws_manager
from rate_limiter import reset as reset_rate_limiter

router = APIRouter(tags=["panic"])


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
    # Mark all pending trades cancelled
    trade_cancel = await db.trades.update_many(
        {**query, "status": "pending"},
        {"$set": {"status": "cancelled", "error": "panic_lock",
                  "close_reason": "panic",
                  "closed_at": now_iso, **stamp}}, session=session,
    )
    # Request close on all open trades — EA will close on next poll
    open_close = await db.trades.update_many(
        {**query, "status": "open"},
        nx.close_command_update("panic", {"idempotency_key": (stamp.get("nl_effect") or {}).get("key"),
                                          "fence": (stamp.get("nl_effect") or {}).get("fence")}, **stamp),
        session=session,
    )
    payload = {
        "bots_disabled": bot_result.modified_count,
        "trades_cancelled": trade_cancel.modified_count,
        "open_trades_marked_for_close": open_close.modified_count,
        "at": now_iso,
    }
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
    """Post-commit side effects, exactly once per outbox row (claimed via CAS)."""
    if not outbox_id:
        return False
    row = await db.ops_outbox.find_one_and_update(
        {"_id": outbox_id, "state": "pending"},
        {"$set": {"state": "publishing", "publishing_at": datetime.now(timezone.utc).isoformat()}})
    if not row:
        return False
    if row.get("user_id"):
        await ws_manager.broadcast(row["user_id"], "panic_lock", row["payload"])
    from canonical_decision import bump_authority_version
    ver = await bump_authority_version(db, "panic", user_id=row.get("user_id"))
    await db.ops_outbox.update_one({"_id": outbox_id}, {"$set": {
        "state": "published", "authority_version": ver, "published_at": datetime.now(timezone.utc).isoformat()}})
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
async def panic_global(user=Depends(get_current_user)):
    """Global panic — every user's bot down. Admin role only."""
    from auth import require_admin
    require_admin(user)
    reset_rate_limiter()  # clear all rate-limit buckets
    return await _disable_all_bots_and_close_trades({})
