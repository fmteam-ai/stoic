"""iter-148 — ADMIN_PASSWORD_FORCE_RESET operator recovery path.

Verifies against a scratch DB:
1. seed creates the admin with the env password.
2. Without the flag, a changed env password is NEVER applied (SEC-001).
3. With the flag, the env password is re-synced ONCE per secret value,
   must_change_password is forced, and stale reset tokens are cleared.
4. Idempotence: with the flag still set and the SAME secret, an in-app
   password change is NOT reverted on the next boot.
"""
import asyncio
import os
import uuid

import pytest

pytestmark = pytest.mark.integration

DB_NAME = f"stoic_test_forcereset_{uuid.uuid4().hex[:8]}"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def _scenario():
    os.environ["DB_NAME"] = DB_NAME
    import database
    database._client = None  # fresh client for the scratch DB
    from auth import hash_password, verify_password
    from database import get_db
    from seed import seed_admin

    email = "admin@stoicaibot.com"
    db = get_db()
    try:
        os.environ["ADMIN_EMAIL"] = email
        os.environ["ADMIN_PASSWORD"] = "first-secret-pw"
        os.environ.pop("ADMIN_PASSWORD_FORCE_RESET", None)
        await seed_admin()
        u = await db.users.find_one({"email": email})
        assert verify_password("first-secret-pw", u["password_hash"])

        # (2) changed secret WITHOUT the flag — never applied
        os.environ["ADMIN_PASSWORD"] = "second-secret-pw"
        await seed_admin()
        u = await db.users.find_one({"email": email})
        assert verify_password("first-secret-pw", u["password_hash"])
        assert not verify_password("second-secret-pw", u["password_hash"])

        # (3) flag set — re-synced once, rotation forced, tokens cleared
        await db.users.update_one(
            {"email": email},
            {"$set": {"password_reset_token": "stale-token",
                      "must_change_password": False}})
        os.environ["ADMIN_PASSWORD_FORCE_RESET"] = "true"
        await seed_admin()
        u = await db.users.find_one({"email": email})
        assert verify_password("second-secret-pw", u["password_hash"])
        assert u["must_change_password"] is True
        assert "password_reset_token" not in u
        assert u.get("admin_env_pw_fingerprint")

        # (4) flag STILL set, same secret — an in-app change survives boots
        await db.users.update_one(
            {"email": email},
            {"$set": {"password_hash": hash_password("user-chosen-pw"),
                      "must_change_password": False}})
        await seed_admin()
        u = await db.users.find_one({"email": email})
        assert verify_password("user-chosen-pw", u["password_hash"])
        assert u["must_change_password"] is False

        # a NEW secret value with the flag applies exactly once again
        os.environ["ADMIN_PASSWORD"] = "third-secret-pw"
        await seed_admin()
        u = await db.users.find_one({"email": email})
        assert verify_password("third-secret-pw", u["password_hash"])
        assert u["must_change_password"] is True
    finally:
        os.environ.pop("ADMIN_PASSWORD_FORCE_RESET", None)
        await db.client.drop_database(DB_NAME)
        database._client = None


def test_admin_password_force_reset_lifecycle():
    _run(_scenario())
