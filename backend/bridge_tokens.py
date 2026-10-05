"""P1-01 — bridge tokens are stored ONLY as keyed hashes (+ last 4 characters).

Storage on `accounts`:
  bridge_token_hash / bridge_token_last4          current token
  bridge_token_prev_hash / bridge_token_prev_expires   15-min grace after a rotation
  bridge_token_retired_hashes: [..]               every token this account ever rotated away
                                                  (S2 — an old EA, not an attacker; never valid)
  bridge_token_suspended.token_hash               SA4 R5 suspension (by hash)
The plaintext is shown exactly once (creation / rotation / installer pairing) and never
persisted. Lookups hash the presented token (HMAC-SHA256 under BRIDGE_TOKEN_HASH_KEY,
falling back to JWT_SECRET) — the EA keeps sending the same token, only storage changes.
`migrate_plaintext()` converts legacy rows at boot and deletes the plaintext fields.
"""
import hashlib
import hmac
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger("bridge_tokens")

HASH_KEY_ENV = "BRIDGE_TOKEN_HASH_KEY"
PLAINTEXT_FIELDS = ("bridge_token", "bridge_token_prev", "bridge_token_retired")


def _key() -> bytes:
    k = os.environ.get(HASH_KEY_ENV) or os.environ.get("JWT_SECRET") or ""
    if not k:
        raise RuntimeError("BRIDGE_TOKEN_HASH_KEY / JWT_SECRET missing — cannot hash bridge tokens")
    return k.encode("utf-8")


def token_hash(token: str) -> str:
    return hmac.new(_key(), str(token or "").encode("utf-8"), hashlib.sha256).hexdigest()


def token_fields(token: str) -> dict:
    """Fields to $set when a NEW token is issued (never the plaintext)."""
    return {"bridge_token_hash": token_hash(token), "bridge_token_last4": str(token)[-4:]}


def masked(acc: dict | None) -> str:
    last4 = (acc or {}).get("bridge_token_last4") or (str((acc or {}).get("bridge_token") or "")[-4:])
    return f"••••••••••••{last4}" if last4 else ""


def has_token(acc: dict | None) -> bool:
    return bool((acc or {}).get("bridge_token_hash") or (acc or {}).get("bridge_token"))


def strip_plaintext(doc: dict) -> dict:
    for k in PLAINTEXT_FIELDS:
        doc.pop(k, None)
    return doc


async def find_by_current(db, token: str):
    """Account whose CURRENT token is `token` (hash first, legacy plaintext until migrated)."""
    acc = await db.accounts.find_one({"bridge_token_hash": token_hash(token)})
    if acc is None:
        acc = await db.accounts.find_one({"bridge_token": token})
    return acc


async def find_by_prev(db, token: str, now_iso: str):
    h = token_hash(token)
    return await db.accounts.find_one({"$or": [{"bridge_token_prev_hash": h}, {"bridge_token_prev": token}],
                                       "bridge_token_prev_expires": {"$gt": now_iso}})


async def is_retired(db, token: str) -> bool:
    """S2 — a token this platform once issued and has since rotated away."""
    h = token_hash(token)
    row = await db.accounts.find_one({"$or": [{"bridge_token_prev_hash": h}, {"bridge_token_retired_hashes": h},
                                              {"bridge_token_prev": token}, {"bridge_token_retired": token}]}, {"_id": 1})
    return row is not None


def is_suspended(acc: dict | None, token: str) -> bool:
    susp = (acc or {}).get("bridge_token_suspended") or {}
    if not susp:
        return False
    return susp.get("token_hash") == token_hash(token) or susp.get("token") == token


def suspension_record(acc: dict, **extra) -> dict:
    """SA4 R5 — suspend the CURRENT token by hash (never store the plaintext)."""
    return {"token_hash": acc.get("bridge_token_hash") or (token_hash(acc["bridge_token"]) if acc.get("bridge_token") else None),
            **extra}


def rotation_update(acc: dict, new_token: str, *, grace_until: str | None, suspended: bool) -> dict:
    """Mongo update for a rotation: new hash, previous → grace (unless suspended), old → retired list."""
    old_hash = acc.get("bridge_token_hash") or (token_hash(acc["bridge_token"]) if acc.get("bridge_token") else None)
    sets = {**token_fields(new_token), "bridge_token_rotated_at": datetime.now(timezone.utc).isoformat(),
            "bridge_token_prev_hash": None if (suspended or not old_hash) else old_hash,
            "bridge_token_prev_expires": grace_until}
    upd: dict = {"$set": sets, "$unset": {"bridge_token": "", "bridge_token_prev": "", "bridge_token_retired": "",
                                          "bridge_token_suspended": ""}}
    if old_hash:
        upd["$addToSet"] = {"bridge_token_retired_hashes": old_hash}
    return upd


async def retire_prev(db, acc: dict) -> None:
    """First use of the rotated token: the previous one is retired immediately (grace closed)."""
    prev_hash = acc.get("bridge_token_prev_hash") or (token_hash(acc["bridge_token_prev"]) if acc.get("bridge_token_prev") else None)
    upd: dict = {"$unset": {"bridge_token_prev_hash": "", "bridge_token_prev": "", "bridge_token_prev_expires": ""}}
    if prev_hash:
        upd["$addToSet"] = {"bridge_token_retired_hashes": prev_hash}
    await db.accounts.update_one({"_id": acc["_id"]}, upd)


async def ensure_indexes(db) -> None:
    """Unique partial index on the hash; the old plaintext unique index would collide on the
    missing field once plaintexts are removed, so it is dropped."""
    try:
        info = await db.accounts.index_information()
        if "bridge_token_1" in info:
            await db.accounts.drop_index("bridge_token_1")
    except Exception as e:  # noqa: BLE001
        logger.warning("bridge_token_1 index drop skipped: %s", type(e).__name__)
    await db.accounts.create_index("bridge_token_hash", unique=True,
                                   partialFilterExpression={"bridge_token_hash": {"$type": "string"}})
    await db.accounts.create_index("bridge_token_prev_hash", sparse=True)
    await db.accounts.create_index("bridge_token_retired_hashes", sparse=True)


async def migrate_plaintext(db) -> int:
    """One-shot, idempotent: hash every plaintext token field and delete the plaintext."""
    n = 0
    async for acc in db.accounts.find({"$or": [{"bridge_token": {"$type": "string"}}, {"bridge_token_prev": {"$type": "string"}},
                                               {"bridge_token_retired": {"$type": "string"}},
                                               {"bridge_token_suspended.token": {"$type": "string"}}]},
                                      {"bridge_token": 1, "bridge_token_prev": 1, "bridge_token_retired": 1,
                                       "bridge_token_suspended": 1, "bridge_token_hash": 1}).limit(100000):
        sets, adds = {}, []
        if acc.get("bridge_token") and not acc.get("bridge_token_hash"):
            sets.update(token_fields(acc["bridge_token"]))
        if acc.get("bridge_token_prev"):
            sets["bridge_token_prev_hash"] = token_hash(acc["bridge_token_prev"])
        if acc.get("bridge_token_retired"):
            adds.append(token_hash(acc["bridge_token_retired"]))
        susp = acc.get("bridge_token_suspended") or {}
        if susp.get("token"):
            sets["bridge_token_suspended"] = {**{k: v for k, v in susp.items() if k != "token"},
                                              "token_hash": token_hash(susp["token"])}
        upd: dict = {"$unset": {"bridge_token": "", "bridge_token_prev": "", "bridge_token_retired": ""}}
        if sets:
            upd["$set"] = sets
        if adds:
            upd["$addToSet"] = {"bridge_token_retired_hashes": {"$each": adds}}
        await db.accounts.update_one({"_id": acc["_id"]}, upd)
        n += 1
    if n:
        logger.warning("P1-01 — migrated %d account(s): bridge tokens are now stored as keyed hashes only", n)
    return n
