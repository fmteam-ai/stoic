"""M119-5 — account deletion hygiene. Deleting an account must take its pairing tokens and its open
alerts with it; otherwise an orphan token keeps a `pairing_no_heartbeat` alert (VPS PAIRING SILENT)
re-firing forever. Shared by the delete routes and the alert evaluator's orphan sweep."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from bson import ObjectId

logger = logging.getLogger("account_cleanup")

ORPHAN_SCAN_LIMIT = 500


def alert_filter(account_id: str) -> dict:
    """Open ops alerts that belong to the account: pairing dedup key or any evaluator meta.account_id."""
    return {"acked_at": None,
            "$or": [{"dedup_key": f"pairing_heartbeat:{account_id}"}, {"meta.account_id": account_id}]}


async def cleanup_deleted_account(db, account_id: str, *, reason: str = "account-deleted") -> dict:
    """Remove the account's pairing tokens and close its open alerts. Idempotent; never touches trades."""
    account_id = str(account_id)
    tokens = await db.pairing_tokens.delete_many({"account_id": account_id})
    now = datetime.now(timezone.utc)
    alerts = await db.ops_alerts.update_many(
        alert_filter(account_id),
        {"$set": {"acked_at": now, "acked_by": f"system:{reason}", "auto_resolved": True, "resolved_at": now}})
    out = {"account_id": account_id, "pairing_tokens_removed": tokens.deleted_count,
           "alerts_closed": alerts.modified_count}
    if tokens.deleted_count or alerts.modified_count:
        logger.info("account %s cleanup (%s): %d pairing token(s) removed, %d alert(s) closed",
                    account_id, reason, tokens.deleted_count, alerts.modified_count)
    return out


async def account_exists(db, account_id) -> bool:
    if not ObjectId.is_valid(str(account_id)):
        return False
    return await db.accounts.find_one({"_id": ObjectId(str(account_id))}, {"_id": 1}) is not None


async def purge_orphan_pairing_tokens(db) -> list:
    """Tokens whose account no longer exists: delete them and close their alerts (bounded per cycle)."""
    seen, out = set(), []
    async for tok in db.pairing_tokens.find({}, {"account_id": 1}).limit(ORPHAN_SCAN_LIMIT):
        aid = str(tok.get("account_id") or "")
        if not aid or aid in seen:
            continue
        seen.add(aid)
        if not await account_exists(db, aid):
            out.append(await cleanup_deleted_account(db, aid, reason="orphan-pairing-token"))
    return out
