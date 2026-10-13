"""U18 (Phase 1) — tombstones for deleted accounts.

When an account is removed on the dashboard the EA on the VPS is still attached and keeps polling with the
old bridge token. Before: every poll was a 401 "invalid token" → B2 token-guessing finding → CRITICAL, and
the operator chased a phantom attacker. Now: the token hashes are kept in `account_tombstones`; the bridge
answers HTTP 410 `account_deleted` (EA 1.64 prints the reason and stops polling), and exactly ONE info
finding per tombstone is recorded. Tombstones carry hashes only — never a token."""
from __future__ import annotations

from datetime import datetime, timezone

TOMBSTONE_TTL_DAYS = 90


def _hashes(acc: dict) -> list:
    out = []
    for k in ("bridge_token_hash", "bridge_token_prev_hash"):
        if acc.get(k):
            out.append(str(acc[k]))
    out.extend(str(h) for h in (acc.get("bridge_token_retired_hashes") or []))
    return sorted(set(out))


async def ensure_indexes(db) -> None:
    """C3 — the bridge scans token_hashes on every unknown token; expired tombstones leave by TTL."""
    await db.account_tombstones.create_index("token_hashes")
    await db.account_tombstones.create_index("expires_at", expireAfterSeconds=0)


async def write_tombstone(db, acc: dict, *, deleted_by: str) -> dict:
    """Also the durable label for the account's broker_deals (the audit trail is never deleted): user, broker,
    environment as the server CLASSIFIED it at deletion time (attestation / paper / broker trade mode — never the
    user-typed field alone). A tombstone that labels deals carries no TTL; token-only tombstones still expire."""
    from broker_env import attested_environment, broker_environment
    now = datetime.now(timezone.utc)
    aid = str(acc["_id"])
    has_deals = await db.broker_deals.count_documents({"account_id": aid}, limit=1) > 0
    expires = datetime.fromtimestamp(now.timestamp() + TOMBSTONE_TTL_DAYS * 86400, tz=timezone.utc)
    doc = {"_id": aid, "token_hashes": _hashes(acc), "label": acc.get("label"), "broker": acc.get("broker"),
           "account_number": acc.get("account_number"), "deleted_at": now.isoformat(), "deleted_by": deleted_by,
           "user_id": acc.get("user_id"), "mode": acc.get("mode"), "account_type": acc.get("account_type"),
           "server": acc.get("broker_server") or acc.get("server"),
           "environment": attested_environment(acc), "declared_environment": broker_environment(acc),
           "has_deals": has_deals,
           "expires_at": None if has_deals else expires,                                 # C3 — BSON date (TTL index); None = never
           "expires_at_iso": None if has_deals else expires.isoformat(),
           "polls_after_delete": 0, "last_poll_at": None, "finding_recorded": False}
    await db.account_tombstones.replace_one({"_id": doc["_id"]}, doc, upsert=True)
    return doc


async def find_by_token_hash(db, token_hash: str) -> dict | None:
    if not token_hash:
        return None
    return await db.account_tombstones.find_one({"token_hashes": token_hash})


async def note_poll(db, tomb: dict, *, ip: str | None) -> bool:
    """Count the poll; return True the FIRST time (caller records the single info finding)."""
    now = datetime.now(timezone.utc).isoformat()
    res = await db.account_tombstones.find_one_and_update(
        {"_id": tomb["_id"], "finding_recorded": {"$ne": True}},
        {"$set": {"finding_recorded": True, "last_poll_at": now, "last_poll_ip": ip}, "$inc": {"polls_after_delete": 1}})
    if res is not None:
        return True
    await db.account_tombstones.update_one({"_id": tomb["_id"]},
                                           {"$set": {"last_poll_at": now, "last_poll_ip": ip}, "$inc": {"polls_after_delete": 1}})
    return False
