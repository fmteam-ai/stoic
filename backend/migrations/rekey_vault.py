"""One-time migration: re-encrypt vault blobs from legacy JWT_SECRET-derived key
to the new KEY_VAULT_MASTER key (SEC-002). Idempotent — skips blobs that already
decrypt under the current key.

Run: cd /app/backend && python -m migrations.rekey_vault
"""
import os
import asyncio
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
from secrets_loader import resolve_file_secrets  # noqa: E402
resolve_file_secrets()  # Docker-secret deployments: MONGO_URL_FILE → MONGO_URL etc.

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402
from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402
import base64  # noqa: E402
import secrets_vault  # noqa: E402
from secrets_vault import _derive_key_from_master, encrypt as vault_encrypt, decrypt as vault_decrypt  # noqa: E402

TARGETS = [
    ("accounts", ["creds.investor", "creds.master"]),
    ("notifications", ["telegram_bot_token"]),
    ("crypto_accounts", ["creds.api_key", "creds.api_secret", "creds.api_passphrase"]),
]


def _get_nested(doc, path):
    cur = doc
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _legacy_decrypt(blob):
    legacy = AESGCM(_derive_key_from_master(os.environ.get("JWT_SECRET", "dev-fallback-master")))
    ct = base64.b64decode(blob["ciphertext"])
    nonce = base64.b64decode(blob["nonce"])
    return legacy.decrypt(nonce, ct, None).decode("utf-8")


async def main():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ["DB_NAME"]]
    migrated = skipped = failed = 0
    for coll_name, paths in TARGETS:
        coll = db[coll_name]
        async for doc in coll.find({}):
            updates = {}
            for path in paths:
                blob = _get_nested(doc, path)
                if not isinstance(blob, dict) or "ciphertext" not in blob:
                    continue
                try:
                    vault_decrypt(blob)
                    skipped += 1
                    continue
                except Exception:
                    pass
                try:
                    plaintext = _legacy_decrypt(blob)
                    updates[path] = vault_encrypt(plaintext)
                except Exception as e:
                    failed += 1
                    print(f"  FAIL {coll_name}/{doc.get('_id')} {path}: {e}")
            if updates:
                await coll.update_one({"_id": doc["_id"]}, {"$set": updates})
                migrated += len(updates)
    print(f"done: migrated={migrated} already-current={skipped} failed={failed}")
    client.close()


if __name__ == "__main__":
    asyncio.run(main())
