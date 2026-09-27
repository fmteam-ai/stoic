"""Admin-only panic-lock endpoint + per-user kill switch.

Two surfaces:
  - POST /api/admin/panic        : global. Closes all pending+open trades,
                                   disables every bot_config. Admin role only.
  - POST /api/panic              : per-user. Same blast radius scoped to the
                                   calling user. Any authenticated user can use it.
"""
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Request

from auth import get_current_user
from database import get_db
from step_up import audit_event
from ws_manager import manager as ws_manager
from rate_limiter import reset as reset_rate_limiter

router = APIRouter(tags=["panic"])


async def _disable_all_bots_and_close_trades(query: dict, broadcast_user_id: str = None) -> dict:
    db = get_db()
    now_iso = datetime.now(timezone.utc).isoformat()
    bot_result = await db.bot_configs.update_many(
        query,
        {"$set": {
            "active": False,
            "tripped_at": now_iso,
            "tripped_reason": "PANIC LOCK — all trading halted by user/admin",
        }},
    )
    # Mark all pending trades cancelled
    trade_cancel = await db.trades.update_many(
        {**query, "status": "pending"},
        {"$set": {"status": "cancelled", "error": "panic_lock",
                  "close_reason": "panic",
                  "closed_at": now_iso}},
    )
    # Request close on all open trades — EA will close on next poll
    open_close = await db.trades.update_many(
        {**query, "status": "open"},
        {"$set": {"close_requested": True, "close_reason": "panic"}},
    )
    payload = {
        "bots_disabled": bot_result.modified_count,
        "trades_cancelled": trade_cancel.modified_count,
        "open_trades_marked_for_close": open_close.modified_count,
        "at": now_iso,
    }
    if broadcast_user_id:
        await ws_manager.broadcast(broadcast_user_id, "panic_lock", payload)
    from canonical_decision import bump_authority_version
    await bump_authority_version(db, "panic", user_id=broadcast_user_id)
    return payload


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
