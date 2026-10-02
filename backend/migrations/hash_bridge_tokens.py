"""One-time migration: bridge tokens at rest → SHA-256 digests (impr-auth).

For every account still carrying a plaintext `bridge_token` (and/or a
rotation-grace `bridge_token_prev`) it stores `bridge_token_hash`
(+ `bridge_token_last4` for the masked UI) / `bridge_token_prev_hash`, then —
only once the stored hash verifies against the plaintext — removes the
plaintext fields. Idempotent: a second run finds nothing to do.

Index handling: the legacy `bridge_token_1` unique index is NOT sparse, so
more than one account without a plaintext token would violate it. The
migration therefore (1) creates a unique PARTIAL index on
`bridge_token_hash` and (2) drops `bridge_token_1` before stripping
plaintext. seed.ensure_indexes must stop re-creating `bridge_token` unique
in the same release (see rollout notes in the PR / report).

Phases:
  python -m migrations.hash_bridge_tokens --hash-only   # safe any time
  python -m migrations.hash_bridge_tokens               # hash + strip plaintext
  python -m migrations.hash_bridge_tokens --dry-run     # counts only

Run: cd /app/backend && python -m migrations.hash_bridge_tokens
"""
import argparse
import asyncio
import hashlib
import os

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

LEGACY_INDEX = "bridge_token_1"
HASH_INDEX = "bridge_token_hash_1"


def _digest(token: str) -> str:
    # Must match auth.hash_bridge_token (kept local so the migration has no
    # app-import side effects).
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _plain(v) -> str | None:
    return v if isinstance(v, str) and v else None


async def ensure_hash_index(db) -> None:
    await db.accounts.create_index(
        "bridge_token_hash", name=HASH_INDEX, unique=True,
        partialFilterExpression={"bridge_token_hash": {"$type": "string"}})


async def drop_legacy_index(db) -> bool:
    try:
        info = await db.accounts.index_information()
    except Exception:
        return False
    if LEGACY_INDEX in info:
        await db.accounts.drop_index(LEGACY_INDEX)
        return True
    return False


async def migrate(db, drop_plaintext: bool = True, dry_run: bool = False) -> dict:
    stats = {"scanned": 0, "hashed": 0, "prev_hashed": 0, "stripped": 0,
             "skipped_mismatch": 0, "index_dropped": False}
    legacy_index_handled = False
    if not dry_run:
        await ensure_hash_index(db)
    query = {"$or": [{"bridge_token": {"$exists": True}},
                     {"bridge_token_prev": {"$exists": True}}]}
    async for doc in db.accounts.find(query):
        stats["scanned"] += 1
        tok = _plain(doc.get("bridge_token"))
        prev = _plain(doc.get("bridge_token_prev"))
        set_doc, unset_doc = {}, {}
        if tok:
            h = _digest(tok)
            if doc.get("bridge_token_hash") != h:
                # Also overwrites a STALE hash left by a legacy writer that
                # rotated only the plaintext — the plaintext is the truth.
                set_doc["bridge_token_hash"] = h
                set_doc["bridge_token_last4"] = tok[-4:]
                stats["hashed"] += 1
        if prev:
            hp = _digest(prev)
            if doc.get("bridge_token_prev_hash") != hp:
                set_doc["bridge_token_prev_hash"] = hp
                stats["prev_hashed"] += 1
        if drop_plaintext:
            # Strip only what is (or is about to be) verifiably hashed.
            if "bridge_token" in doc:
                if tok is None or (set_doc.get("bridge_token_hash")
                                   or doc.get("bridge_token_hash")) == _digest(tok):
                    unset_doc["bridge_token"] = ""
                else:
                    stats["skipped_mismatch"] += 1
            if "bridge_token_prev" in doc:
                if prev is None or (set_doc.get("bridge_token_prev_hash")
                                    or doc.get("bridge_token_prev_hash")) == _digest(prev):
                    unset_doc["bridge_token_prev"] = ""
            if unset_doc:
                stats["stripped"] += 1
        if dry_run or not (set_doc or unset_doc):
            continue
        if drop_plaintext and unset_doc and not legacy_index_handled:
            # Hashes must exist BEFORE plaintext goes; the non-sparse legacy
            # unique index must go before >1 doc lacks the field.
            stats["index_dropped"] = await drop_legacy_index(db)
            legacy_index_handled = True
        upd = {}
        if set_doc:
            upd["$set"] = set_doc
        if unset_doc:
            upd["$unset"] = unset_doc
        await db.accounts.update_one({"_id": doc["_id"]}, upd)
    return stats


async def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--hash-only", action="store_true",
                    help="write hashes but keep plaintext (phase 1)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    from motor.motor_asyncio import AsyncIOMotorClient
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ["DB_NAME"]]
    stats = await migrate(db, drop_plaintext=not args.hash_only,
                          dry_run=args.dry_run)
    print(f"hash_bridge_tokens: {stats}")
    client.close()


if __name__ == "__main__":
    asyncio.run(main())
