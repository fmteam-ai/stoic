"""Iter-81 — v1.6.0 release verification.

Covers:
1. Admin login returns `must_change_password: false` in preview (no ADMIN_PASSWORD_FILE).
2. Change-password flow (register → verify email via Mongo → change → verify persistence).
3. Journal card GET/POST works + LLM output is Pydantic-validated (no 500).
4. Public journal link revocation: share -> fetch -> revoke -> confirm 404.
5. /api/ops/release-readiness (a.k.a. readiness) includes worker loop telemetry.
6. Regression: /api/auth/me, /api/accounts.
"""
import os
import time
import uuid
import pytest
import requests
from pymongo import MongoClient
from bson import ObjectId

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://stoic-trading-bot.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "ai_trading_bot")
STEP_UP_BYPASS = os.environ.get("STEP_UP_BYPASS_TOKEN", "")


# ----------------- Fixtures -----------------
@pytest.fixture(scope="module")
def mongo():
    client = MongoClient(MONGO_URL)
    return client[DB_NAME]


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": "admin@trading.bot", "password": "admin123"},
               timeout=15)
    assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text}"
    return s, r.json()


# ----------------- 1. Admin login -----------------
class TestAdminLogin:
    def test_admin_login_flag_false_in_preview(self, admin_session):
        _, body = admin_session
        # must_change_password must be PRESENT and FALSE in preview (no ADMIN_PASSWORD_FILE)
        assert "must_change_password" in body, "must_change_password field missing on login"
        assert body["must_change_password"] is False, \
            f"Expected must_change_password=False in preview, got {body['must_change_password']}"
        assert body["email"] == "admin@trading.bot"
        assert body["role"] == "admin"

    def test_admin_me_carries_flag(self, admin_session):
        s, _ = admin_session
        r = s.get(f"{API}/auth/me", timeout=10)
        assert r.status_code == 200
        me = r.json()
        assert "must_change_password" in me
        assert me["must_change_password"] is False


# ----------------- 2. Change-password full flow -----------------
class TestChangePassword:
    @pytest.fixture(scope="class")
    def fresh_user(self, mongo):
        email = f"iter81_{uuid.uuid4().hex[:10]}@example.com"
        pw_old = "OldKd5Zt9mW2xVpR7c!"
        pw_new = "NewQr8Ht4nB6vJk3Fs!"
        # Register
        r = requests.post(f"{API}/auth/register",
                          json={"email": email, "password": pw_old,
                                "name": "iter81 user", "terms_agreed": True},
                          timeout=15)
        assert r.status_code == 200, f"register failed {r.status_code}: {r.text}"
        # Verify email by flipping in Mongo
        res = mongo.users.update_one(
            {"email": email},
            {"$set": {"email_verified": True},
             "$unset": {"activation_token": ""}}
        )
        assert res.modified_count == 1
        yield {"email": email, "old": pw_old, "new": pw_new}
        # cleanup
        mongo.users.delete_one({"email": email})

    def test_login_change_password_and_verify(self, fresh_user):
        s = requests.Session()
        # Login with old
        r = s.post(f"{API}/auth/login",
                   json={"email": fresh_user["email"], "password": fresh_user["old"]},
                   timeout=15)
        assert r.status_code == 200, f"initial login failed: {r.status_code} {r.text}"
        body = r.json()
        assert body["must_change_password"] is False

        # Change password
        r = s.post(f"{API}/auth/change-password",
                   json={"current_password": fresh_user["old"],
                         "new_password": fresh_user["new"]},
                   timeout=15)
        assert r.status_code == 200, f"change-password failed: {r.status_code} {r.text}"
        assert r.json().get("ok") is True

        # Old password rejected (fresh session — old session cookies were revoked)
        s2 = requests.Session()
        r = s2.post(f"{API}/auth/login",
                    json={"email": fresh_user["email"], "password": fresh_user["old"]},
                    timeout=15)
        assert r.status_code == 401, f"expected 401 with old pw, got {r.status_code}"

        # New password accepted
        s3 = requests.Session()
        r = s3.post(f"{API}/auth/login",
                    json={"email": fresh_user["email"], "password": fresh_user["new"]},
                    timeout=15)
        assert r.status_code == 200, f"new-pw login failed: {r.status_code} {r.text}"
        # /me should still show flag false
        r_me = s3.get(f"{API}/auth/me", timeout=10)
        assert r_me.status_code == 200
        assert r_me.json().get("must_change_password") is False


# ----------------- 3. Journal cards -----------------
class TestJournalCards:
    def test_journal_card_schema_when_present(self, admin_session, mongo):
        # Look for at least one existing card in DB and validate its schema is well-formed
        doc = mongo.trade_journal_cards.find_one({})
        if not doc:
            pytest.skip("No journal cards in DB — nothing to schema-check")
        card = doc.get("card") or {}
        # Must have all Pydantic-required fields with proper types/ranges
        required = ["title", "verdict", "summary", "grade", "hashtags"]
        for f in required:
            assert f in card, f"Missing field {f} in cached card {doc.get('_id')}"
        assert card["verdict"] in ("WIN", "LOSS", "SCRATCH"), f"Bad verdict: {card['verdict']}"
        assert card["grade"] in ("A", "B", "C", "D", "F"), f"Bad grade: {card['grade']}"
        assert isinstance(card["hashtags"], list)
        assert len(card["hashtags"]) <= 5
        assert len(card["title"]) <= 120
        assert len(card["summary"]) <= 600

    def test_get_card_endpoint_for_owned_trade(self, admin_session, mongo):
        s, admin = admin_session
        # Find a card owned by admin
        doc = mongo.trade_journal_cards.find_one({"user_id": admin["id"]})
        if not doc:
            pytest.skip("Admin has no journal cards")
        trade_id = doc["trade_id"]
        r = s.get(f"{API}/journal/{trade_id}/card", timeout=15)
        assert r.status_code == 200, f"GET journal card failed: {r.status_code} {r.text}"
        body = r.json()
        assert body["trade_id"] == trade_id
        assert "card" in body
        # If LLM was consulted, no 500 was raised — validated schema
        card = body["card"]
        if card:  # not empty
            assert card.get("verdict") in ("WIN", "LOSS", "SCRATCH"), card
            assert card.get("grade") in ("A", "B", "C", "D", "F"), card


# ----------------- 4. Public journal revocation -----------------
class TestPublicJournalRevocation:
    def test_share_then_revoke_returns_404(self, admin_session, mongo):
        s, admin = admin_session
        # Find an owned closed trade with a card
        doc = mongo.trade_journal_cards.find_one({"user_id": admin["id"]})
        if not doc:
            pytest.skip("Admin has no journal cards — cannot test share/revoke flow")
        trade_id = doc["trade_id"]

        # Snapshot original state so we can restore
        original_share_id = doc.get("share_id")
        original_revoked = bool(doc.get("revoked"))

        try:
            # Enable/rotate share
            r = s.post(f"{API}/journal/{trade_id}/share", timeout=15)
            assert r.status_code == 200, f"share failed: {r.status_code} {r.text}"
            share_id = r.json()["share_id"]
            assert share_id, "share_id empty after enable"

            # Unauthenticated fetch works
            r_pub = requests.get(f"{API}/public/journal/{share_id}", timeout=10)
            assert r_pub.status_code == 200, f"public fetch failed: {r_pub.status_code}"
            assert "card" in r_pub.json()

            # Revoke
            r_rev = s.delete(f"{API}/journal/{trade_id}/card", timeout=15)
            assert r_rev.status_code == 200, f"revoke failed: {r_rev.status_code} {r_rev.text}"

            # After revoke, public URL must return 404 (revoked flag flipped)
            r_pub2 = requests.get(f"{API}/public/journal/{share_id}", timeout=10)
            assert r_pub2.status_code == 404, \
                f"Expected 404 after revoke, got {r_pub2.status_code}: {r_pub2.text}"

            # And re-share issues a NEW share_id (old must stay 404 permanently)
            r_re = s.post(f"{API}/journal/{trade_id}/share", timeout=15)
            assert r_re.status_code == 200
            new_share_id = r_re.json()["share_id"]
            assert new_share_id != share_id, "Re-share must issue a NEW share_id"
            # Old share_id remains dead
            r_dead = requests.get(f"{API}/public/journal/{share_id}", timeout=10)
            assert r_dead.status_code == 404, \
                f"Old (revoked) share_id must never resurrect, got {r_dead.status_code}"
        finally:
            # Restore original state
            mongo.trade_journal_cards.update_one(
                {"trade_id": trade_id, "user_id": admin["id"]},
                {"$set": {"share_id": original_share_id,
                          "revoked": original_revoked}}
            )


# ----------------- 5. Readiness with worker telemetry -----------------
class TestReadinessWithWorkerTelemetry:
    def test_release_readiness_returns_payload_with_loops(self, admin_session):
        s, _ = admin_session
        r = s.get(f"{API}/ops/release-readiness", timeout=15)
        # In preview, worker leases missing → 503 with body; 200 in a healthy deploy
        assert r.status_code in (200, 503), \
            f"unexpected status {r.status_code}: {r.text}"
        body = r.json()
        assert "ready" in body
        assert "checks" in body
        assert "workers" in body["checks"]
        workers_detail = body["checks"]["workers"].get("detail") or {}
        # every expected worker slot must be present
        for wname in ("trading", "protection", "reconciliation",
                      "analytics", "model", "tuning"):
            assert wname in workers_detail, f"Missing worker: {wname}"
            # worker entry contains loop-telemetry-derived fields
            entry = workers_detail[wname]
            assert "alive" in entry
            assert "crashlooping" in entry
            # loops is either "n/m" string or None (legacy leases)
            assert "loops" in entry


# ----------------- 6. Regression smoke -----------------
class TestRegressionSmoke:
    def test_auth_me(self, admin_session):
        s, _ = admin_session
        r = s.get(f"{API}/auth/me", timeout=10)
        assert r.status_code == 200
        me = r.json()
        assert me["email"] == "admin@trading.bot"
        assert me["role"] == "admin"

    def test_accounts_list(self, admin_session):
        s, _ = admin_session
        r = s.get(f"{API}/accounts", timeout=15)
        assert r.status_code == 200, f"accounts list failed: {r.status_code} {r.text}"
        body = r.json()
        # Accepts list or dict wrapper
        assert isinstance(body, (list, dict))


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
