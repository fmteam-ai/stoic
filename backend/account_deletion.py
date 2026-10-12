"""P1-01 (main131) — account deletion discipline.

Normal DELETE /api/accounts/{id} has NO force escape hatch any more: it refuses while the account carries
open / pending / UNKNOWN trades or while its broker truth is stale (last heartbeat older than STALE_S). An
account that never connected and never traded has nothing to reconcile and may be deleted. Everything else
goes through the admin-only POST /api/admin/accounts/{id}/force-delete (step-up MFA, fresh reconciliation,
explicit position disposition + reason, audit chain entry).
"""
from __future__ import annotations

from datetime import datetime, timezone

STALE_S = 600                                   # 10 min — broker truth older than this cannot vouch for "no open positions"
DISPOSITIONS = ("positions_closed_on_broker", "leave_positions_to_broker", "no_positions")


def _age_s(hb, now: datetime) -> float | None:
    if not hb:
        return None
    try:
        dt = hb if isinstance(hb, datetime) else datetime.fromisoformat(str(hb).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (now - dt).total_seconds()
    except (TypeError, ValueError):
        return None


def delete_refusal(acc: dict, *, non_terminal: int, unknown: int, ever_traded: bool, now: datetime | None = None) -> dict | None:
    """Pure. None = the normal delete may proceed; otherwise {'code','message'} for a 409."""
    now = now or datetime.now(timezone.utc)
    if non_terminal:
        return {"code": "trades_attached", "message": f"{non_terminal} open/pending trade(s) are still attached. Sync with the broker or close them first; an admin can force-delete."}
    if unknown:
        return {"code": "unknown_execution_state", "message": f"{unknown} execution intent(s) are in UNKNOWN state — the broker outcome is not known. An admin must force-delete after a fresh reconciliation."}
    age = _age_s(acc.get("last_heartbeat"), now)
    if age is None and not ever_traded:
        return None                                # never connected, never traded — nothing to reconcile
    if age is None or age > STALE_S:
        return {"code": "stale_broker_truth", "message": "The terminal has not reported in the last 10 minutes, so open positions cannot be ruled out. Reconnect the EA and retry, or ask an admin to force-delete."}
    return None


async def purge_account(db, acc: dict, *, deleted_by: str) -> dict:
    """Shared tail of both delete paths: tombstone (EA gets 410), row, bot configs, pairing tokens + alerts."""
    from account_tombstones import write_tombstone
    from account_cleanup import cleanup_deleted_account
    aid = str(acc["_id"])
    await write_tombstone(db, acc, deleted_by=deleted_by)
    await db.accounts.delete_one({"_id": acc["_id"]})
    await db.bot_configs.delete_many({"account_id": aid})
    cleanup = await cleanup_deleted_account(db, aid)
    return {k: v for k, v in cleanup.items() if k != "account_id"}
