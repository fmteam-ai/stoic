"""U11 (Phase 1) — one bot per account, never an orphan.

· `ensure_account_bot(db, user_id, account_id)` — called at account creation: exactly one bot_config bound
  to the account, default profile, inactive until the user presses Start.
· `delete_orphan_bot_configs(db)` — startup migration: removes account-less configs (the old per-user
  "default profile") and configs whose account no longer exists. Counts are logged and audited."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from bson import ObjectId

logger = logging.getLogger(__name__)


async def ensure_account_bot(db, user_id: str, account_id: str) -> dict:
    from routes.bot_routes import _get_or_create_config
    return await _get_or_create_config(db, user_id, account_id)   # inactive by default — Start is the user's third step


async def delete_orphan_bot_configs(db, *, reason: str = "migration") -> dict:
    """M4 (main127) — NEVER deletes an ACTIVE config. Inactive orphans (account-less default profiles / configs of
    accounts that no longer exist) are moved to `bot_configs_orphaned` (quarantine, restorable) rather than
    dropped; active orphans are left in place, counted and reported so an operator decides."""
    account_ids: set[str] = set()
    async for a in db.accounts.find({}, {"_id": 1}):
        account_ids.add(str(a["_id"]))
    null_q = {"$or": [{"account_id": None}, {"account_id": {"$exists": False}}]}
    quarantine, active_kept = [], 0
    async for c in db.bot_configs.find({"$or": [null_q, {"account_id": {"$type": "string"}}]}):
        aid = c.get("account_id")
        orphan = not aid or aid not in account_ids
        if not orphan:
            continue
        if c.get("active"):
            active_kept += 1
            continue
        quarantine.append(c)
    null_n = sum(1 for c in quarantine if not c.get("account_id"))
    if quarantine:
        now = datetime.now(timezone.utc).isoformat()
        await db.bot_configs_orphaned.insert_many([{**c, "orphaned_at": now, "orphaned_reason": reason} for c in quarantine])
        await db.bot_configs.delete_many({"_id": {"$in": [c["_id"] for c in quarantine]}})
    out = {"quarantined_null_account": null_n, "quarantined_unknown_account": len(quarantine) - null_n,
           "active_orphans_kept": active_kept, "reason": reason}
    if quarantine or active_kept:
        logger.info("U11/M4 orphan bot configs: %s", out)
        if active_kept:
            logger.warning("U11/M4: %d ACTIVE orphan bot config(s) left in place — inventory reports them; an operator must decide", active_kept)
        try:
            from audit_chain import append_chained
            await append_chained(db, {"actor_email": "system", "action": "bot_configs_orphans_quarantined", "target_kind": "platform",
                                      "target_id": "bot_configs", "reason": reason,
                                      "at": datetime.now(timezone.utc).isoformat(), "meta": out})
        except Exception:  # noqa: BLE001 — the cleanup itself is what matters at startup
            logger.exception("U11 audit entry failed")
    return out


def is_valid_oid(v) -> bool:
    try:
        ObjectId(str(v))
        return True
    except Exception:  # noqa: BLE001
        return False
