"""iter-177 review — endpoint contract/authz/negative-path tests for
   WebAuthn passkeys, ops (agent-certs, turnstile-diag), and infra
   rotate-credentials. Positive full-passkey ceremony already covered by
   tests/test_iter177_webauthn_passkeys.py — do NOT duplicate here."""
import os
import secrets
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")


def _login(email: str, password: str) -> requests.Session:
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": email, "password": password})
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    return s


@pytest.fixture(scope="module")
def admin() -> requests.Session:
    return _login("admin@stoicaibot.com", "admin123")


@pytest.fixture(scope="module")
def anon() -> requests.Session:
    return requests.Session()


# ────────────────────────── WebAuthn passkey routes ─────────────────────────
class TestWebAuthnAuthZ:
    def test_credentials_anon_401_or_403(self, anon):
        r = anon.get(f"{BASE_URL}/api/auth/webauthn/credentials")
        assert r.status_code in (401, 403), f"got {r.status_code}"

    def test_credentials_admin_200_list(self, admin):
        r = admin.get(f"{BASE_URL}/api/auth/webauthn/credentials")
        assert r.status_code == 200
        data = r.json()
        assert "credentials" in data
        assert isinstance(data["credentials"], list)

    def test_register_begin_anon_401_or_403(self, anon):
        r = anon.post(f"{BASE_URL}/api/auth/webauthn/register/begin", json={})
        assert r.status_code in (401, 403)

    def test_register_begin_admin_ok_rp_from_origin(self, admin):
        body = {"origin": "https://passkey-test.example.com"}
        r = admin.post(f"{BASE_URL}/api/auth/webauthn/register/begin",
                       json=body)
        assert r.status_code == 200, r.text[:200]
        data = r.json()
        assert "challenge_id" in data
        assert "options" in data
        opts = data["options"]
        # rp.id derived from origin hostname
        assert opts.get("rp", {}).get("id") == "passkey-test.example.com"
        assert "challenge" in opts

    def test_register_complete_garbage_400(self, admin):
        # begin first to get a challenge_id
        begin = admin.post(f"{BASE_URL}/api/auth/webauthn/register/begin",
                           json={"origin": "https://x.example.com"}).json()
        r = admin.post(f"{BASE_URL}/api/auth/webauthn/register/complete",
                       json={"challenge_id": begin["challenge_id"],
                             "credential": {"garbage": True},
                             "label": "junk"})
        assert r.status_code == 400
        assert "Passkey registration failed" in r.text or "failed" in r.text.lower()

    def test_step_up_begin_unknown_action_400(self, admin):
        r = admin.post(f"{BASE_URL}/api/auth/webauthn/step-up/begin",
                       json={"action": "totally_not_an_action",
                             "origin": "https://x.example.com"})
        assert r.status_code == 400

    def test_step_up_begin_no_passkeys_enrolled_400(self, admin):
        # Ensure admin has no passkey creds first
        creds = admin.get(f"{BASE_URL}/api/auth/webauthn/credentials").json()
        if creds["credentials"]:
            pytest.skip("admin already has passkeys enrolled in preview DB")
        r = admin.post(f"{BASE_URL}/api/auth/webauthn/step-up/begin",
                       json={"action": "audit_anchor",
                             "origin": "https://x.example.com"})
        assert r.status_code == 400
        assert "no passkeys" in r.text.lower() or "enroll" in r.text.lower()

    def test_step_up_complete_invalid_credential_401(self, admin):
        r = admin.post(f"{BASE_URL}/api/auth/webauthn/step-up/complete",
                       json={"action": "audit_anchor",
                             "challenge_id": "bogus",
                             "credential": {"nope": True}})
        assert r.status_code == 401, r.text[:200]


# ─────────────────────────── /ops/agent-certs ───────────────────────────────
class TestAgentCerts:
    def test_anon_403(self, anon):
        r = anon.get(f"{BASE_URL}/api/ops/agent-certs")
        assert r.status_code == 403

    def test_admin_200_shape(self, admin):
        r = admin.get(f"{BASE_URL}/api/ops/agent-certs")
        assert r.status_code == 200, r.text[:200]
        data = r.json()
        for k in ("renew_window_days", "expired", "expiring_soon"):
            assert k in data, f"missing key {k}: {data}"
        assert isinstance(data["expired"], list)
        assert isinstance(data["expiring_soon"], list)


# ─────────────────────────── /ops/turnstile-diag ────────────────────────────
class TestTurnstileDiag:
    def test_anon_403(self, anon):
        r = anon.get(f"{BASE_URL}/api/ops/turnstile-diag")
        assert r.status_code == 403

    def test_admin_200_shape(self, admin):
        r = admin.get(f"{BASE_URL}/api/ops/turnstile-diag")
        assert r.status_code == 200, r.text[:200]
        data = r.json()
        for k in ("enabled", "site_key_set", "secret_key_set",
                  "secret_check", "recent_rejections"):
            assert k in data, f"missing key {k}: {data}"


# ─────────────────── /infra/agents/{id}/rotate-credentials ──────────────────
class TestRotateAgentCredentials:
    def test_admin_rotate_new_token_and_key(self, admin):
        # Seed a synthetic agent so we have something to rotate
        from datetime import datetime, timezone
        import pymongo
        mongo = pymongo.MongoClient(os.environ.get("MONGO_URL",
                                                   "mongodb://localhost:27017"))
        db_name = os.environ.get("DB_NAME", "test_database")
        agent_id = f"TEST_agt_{secrets.token_hex(6)}"
        # find admin user_id via /auth/me
        me = admin.get(f"{BASE_URL}/api/auth/me").json()
        mongo[db_name].vps_agents.insert_one({
            "agent_id": agent_id,
            "user_id": me["id"],
            "agent_token_hash": "seedhash",
            "command_key_enc": "seedenc",
            "revoked": False,
            "created_at": datetime.now(timezone.utc).isoformat()})
        try:
            r = admin.post(
                f"{BASE_URL}/api/infra/agents/{agent_id}/rotate-credentials")
            assert r.status_code == 200, r.text[:300]
            data = r.json()
            assert data["agent_id"] == agent_id
            assert data.get("agent_token", "").startswith("agt_tok_")
            assert isinstance(data.get("command_key"), str)
            assert len(data["command_key"]) >= 32
            # Verify DB no longer stores plaintext token/key
            doc = mongo[db_name].vps_agents.find_one({"agent_id": agent_id})
            assert "agent_token" not in doc
            assert "command_key" not in doc
            assert doc.get("agent_token_hash") != "seedhash"
            assert doc.get("command_key_enc") != "seedenc"
        finally:
            mongo[db_name].vps_agents.delete_one({"agent_id": agent_id})

    def test_rotate_missing_agent_404(self, admin):
        r = admin.post(
            f"{BASE_URL}/api/infra/agents/does-not-exist-zzz/rotate-credentials")
        assert r.status_code in (403, 404), r.text[:200]


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
