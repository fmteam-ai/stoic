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


def test_consumers_classify_per_token_not_globally():
    """r26 P3-01: the failure class comes from the token's own schema epoch —
    no lookup of OTHER users' legacy markers."""
    src = open("routes/auth_routes.py").read()
    assert src.count("raise await _token_failure(") == 2                # both consumers share one classifier
    assert "legacy_activation_invalidated_at" not in src and "legacy_reset_invalidated_at" not in src
    helper = src[src.index("async def _token_failure("):src.index('@router.post("/verify-email")')]
    assert "startswith(TOKEN_SCHEMA)" in helper and "logger" not in helper   # token never logged
    assert "await invalidate_legacy_plaintext_tokens()" in open("seed.py").read()


def test_new_links_carry_the_schema_epoch():
    from activation import new_activation_token, TOKEN_SCHEMA
    from password_reset import new_reset_token
    assert TOKEN_SCHEMA == "v2."
    assert new_activation_token()[0].startswith("v2.") and new_reset_token()[0].startswith("v2.")


def test_failure_classification_matrix():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch
    import routes.auth_routes as ar
    db = MagicMock(); db.auth_token_failures.update_one = AsyncMock()
    with patch.object(ar, "get_db", return_value=db):
        legacy = asyncio.run(ar._token_failure("activation", "abc123", "Activation link", "request a new one"))
        current = asyncio.run(ar._token_failure("activation", "v2.abc123", "Activation link", "request a new one"))
    assert legacy.detail["token_schema"] == "legacy" and legacy.detail["legacy_links_invalidated"] is True
    assert "issued before the security upgrade" in legacy.detail["message"]
    assert current.detail["token_schema"] == "v2" and current.detail["legacy_links_invalidated"] is False
    assert "security upgrade" not in current.detail["message"]
    assert legacy.detail["code"] == current.detail["code"] == "invalid_token"
    schemas = [c.args[0]["schema"] for c in db.auth_token_failures.update_one.await_args_list]
    assert schemas == ["legacy", "v2"]
    for c in db.auth_token_failures.update_one.await_args_list:            # no token material persisted
        assert "abc123" not in str(c)
