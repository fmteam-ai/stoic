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
    account_ids: set[str] = set()
    async for a in db.accounts.find({}, {"_id": 1}):
        account_ids.add(str(a["_id"]))
    null_q = {"$or": [{"account_id": None}, {"account_id": {"$exists": False}}]}
    null_n = await db.bot_configs.count_documents(null_q)
    unknown_ids = []
    async for c in db.bot_configs.find({"account_id": {"$type": "string"}}, {"account_id": 1}):
        if c["account_id"] not in account_ids:
            unknown_ids.append(c["_id"])
    if null_n:
        await db.bot_configs.delete_many(null_q)
    if unknown_ids:
        await db.bot_configs.delete_many({"_id": {"$in": unknown_ids}})
    out = {"deleted_null_account": null_n, "deleted_unknown_account": len(unknown_ids), "reason": reason}
    if null_n or unknown_ids:
        logger.info("U11 orphan bot configs removed: %s", out)
        try:
            from audit_chain import append_chained
            await append_chained(db, {"actor_email": "system", "action": "bot_configs_orphans_deleted", "target_kind": "platform",
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
