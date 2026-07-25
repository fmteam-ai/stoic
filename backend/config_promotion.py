"""Immutable configuration promotion (iter-104, safety review).

Architecture: immutable version → validation → atomic active-pointer →
rollback pointer. The active bot_config document remains the read model the
runtime consumes, but every mutation records an immutable post-state version
in `config_versions` and advances a pointer (`config_pointers`) that always
knows the active version and the one before it. Rollback re-applies the
previous version's content — with a mode-rank guard so a rollback can never
silently RAISE operational authority (mode promotions only via the explicit
certification-gated path).
"""
import hashlib
import json
import logging
from datetime import datetime, timezone

from bson import ObjectId

logger = logging.getLogger("config-promotion")

PROTECTED = {"_id", "user_id", "account_id", "created_at",
             "mode_explicitly_promoted"}


def _pointer_id(user_id: str, account_id: str | None) -> str:
    return f"{user_id}:{account_id or 'default'}"


def _cfg_filter(user_id: str, account_id: str | None) -> dict:
    if account_id:
        return {"user_id": user_id, "account_id": account_id}
    return {"user_id": user_id,
            "$or": [{"account_id": None}, {"account_id": {"$exists": False}}]}


async def record_version(db, cfg: dict | None, label: str,
                         source: str = "config_update") -> str | None:
    """Snapshot the given (post-mutation) config as an immutable version and
    advance the pointer. Content-hash dedup — unchanged config, no new doc."""
    if not cfg:
        return None
    user_id, account_id = cfg.get("user_id"), cfg.get("account_id")
    body = {k: v for k, v in cfg.items() if k != "_id"}
    canonical = json.dumps(body, sort_keys=True, default=str,
                           separators=(",", ":"))
    h = hashlib.sha256(canonical.encode()).hexdigest()
    pid = _pointer_id(user_id, account_id)
    ptr = await db.config_pointers.find_one({"_id": pid}) or {}
    if ptr.get("active_hash") == h:
        return ptr.get("active_version_id")
    res = await db.config_versions.insert_one({
        "user_id": user_id, "account_id": account_id, "label": label,
        "source": source, "config": body, "config_hash": h,
        "immutable": True, "created_at": datetime.now(timezone.utc)})
    vid = str(res.inserted_id)
    await db.config_pointers.update_one(
        {"_id": pid},
        {"$set": {"user_id": user_id, "account_id": account_id,
                  "active_version_id": vid, "active_hash": h,
                  "previous_version_id": ptr.get("active_version_id"),
                  "updated_at": datetime.now(timezone.utc),
                  "source": source}},
        upsert=True)
    return vid


async def rollback(db, user_id: str, account_id: str | None,
                   actor: str = "user") -> dict:
    """Atomically point back to the previous immutable version and re-apply
    its content. The pointer swap keeps roll-forward possible."""
    pid = _pointer_id(user_id, account_id)
    ptr = await db.config_pointers.find_one({"_id": pid, "user_id": user_id})
    prev_id = (ptr or {}).get("previous_version_id")
    if not prev_id:
        raise ValueError("no previous configuration version to roll back to")
    ver = await db.config_versions.find_one(
        {"_id": ObjectId(prev_id), "user_id": user_id})
    if not ver:
        raise ValueError("rollback version not found")
    body = {k: v for k, v in (ver.get("config") or {}).items()
            if k not in PROTECTED}

    # validation — a rollback may never RAISE operational authority
    from change_governance import MODE_RANK
    cur = await db.bot_configs.find_one(_cfg_filter(user_id, account_id)) or {}
    cur_mode = cur.get("operational_mode") or "observe"
    tgt_mode = body.get("operational_mode") or cur_mode
    mode_guard = False
    if MODE_RANK.get(tgt_mode, 0) > MODE_RANK.get(cur_mode, 0):
        body["operational_mode"] = cur_mode
        mode_guard = True
    now = datetime.now(timezone.utc)
    body["updated_at"] = now.isoformat()

    await db.bot_configs.update_one(_cfg_filter(user_id, account_id),
                                    {"$set": body})
    await db.config_pointers.update_one(
        {"_id": pid},
        {"$set": {"active_version_id": prev_id,
                  "active_hash": ver.get("config_hash"),
                  "previous_version_id": ptr.get("active_version_id"),
                  "updated_at": now, "source": f"rollback:{actor}"}})
    await db.audit_log.insert_one({
        "user_id": user_id, "action": "config_rollback",
        "detail": {"account_id": account_id, "rolled_back_to": prev_id,
                   "config_hash": ver.get("config_hash"),
                   "mode_guard_applied": mode_guard},
        "step_up_verified": True, "at": now})
    logger.warning("config rollback user=%s account=%s → version %s "
                   "(mode_guard=%s)", user_id, account_id, prev_id,
                   mode_guard)
    return {"rolled_back_to": prev_id,
            "config_hash": ver.get("config_hash"),
            "mode_guard_applied": mode_guard,
            "roll_forward_version_id": ptr.get("active_version_id")}
