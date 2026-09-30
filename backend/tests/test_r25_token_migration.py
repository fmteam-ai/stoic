"""r25 P2-03 — pre-v96 plaintext activation/reset tokens are invalidated explicitly
at startup; consumers of a legacy link get `token_superseded` with a resend path;
failures are counted per schema without logging the token."""
import asyncio
import os
import uuid

import pytest
from motor.motor_asyncio import AsyncIOMotorClient

import seed

pytestmark = pytest.mark.skipif(not os.environ.get("MONGO_URL"), reason="needs Mongo")


def test_legacy_tokens_invalidated_once(monkeypatch):
    async def run():
        db = AsyncIOMotorClient(os.environ["MONGO_URL"])[f"r25_tok_{uuid.uuid4().hex[:8]}"]
        monkeypatch.setattr(seed, "get_db", lambda: db)
        try:
            await db.users.insert_many([
                {"email": "a@x.com", "activation_token": "plain-a", "activation_expires_at": "2099"},
                {"email": "b@x.com", "password_reset_token": "plain-b", "password_reset_expires_at": "2099"},
                {"email": "c@x.com", "activation_token": "keep", "activation_token_sha256": "h"},   # v96 doc: untouched
            ])
            r = await seed.invalidate_legacy_plaintext_tokens()
            assert r == {"activation": 1, "reset": 1}
            a = await db.users.find_one({"email": "a@x.com"}); b = await db.users.find_one({"email": "b@x.com"})
            assert "activation_token" not in a and "legacy_activation_invalidated_at" in a
            assert "password_reset_token" not in b and "legacy_reset_invalidated_at" in b
            assert (await db.users.find_one({"email": "c@x.com"}))["activation_token"] == "keep"
            assert await seed.invalidate_legacy_plaintext_tokens() == {"activation": 0, "reset": 0}   # idempotent
        finally:
            await db.client.drop_database(db.name)
    asyncio.run(run())


def test_consumers_report_superseded_and_count_by_schema():
    src = open("routes/auth_routes.py").read()
    assert src.count('"legacy_links_invalidated": bool(legacy)') == 2   # canonical code kept; resend hint added
    assert src.count('db.auth_token_failures.update_one') == 2
    assert "payload.token" not in src[src.index("auth_token_failures"):src.index("auth_token_failures") + 400]   # never logged
    assert "await invalidate_legacy_plaintext_tokens()" in open("seed.py").read()
