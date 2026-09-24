from live_target import ADMIN_EMAIL, ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
"""iter-155 — Cloudflare edge security: Turnstile gate, security headers,
CF-Connecting-IP handling, session idle timeout, new-IP login alerts.
"""
import os
import sys
from datetime import datetime, timezone, timedelta

import pytest
import requests
from bson import ObjectId

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)
from dotenv import load_dotenv
load_dotenv(os.path.join(_BACKEND_DIR, ".env"))

from live_target import require_live_base_url
BASE_URL = require_live_base_url()
API = f"{BASE_URL}/api"
pass  # ADMIN_EMAIL comes from live_target
ADMIN_PW = ADMIN_PASSWORD
TIMEOUT = 20


def _run(coro):
    from conftest import run_async
    return run_async(coro)


def _db():
    from database import get_db
    return get_db()


def _admin():
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PW},
               timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return s


# ─── security headers ────────────────────────────────────────────────
def test_api_responses_carry_security_headers():
    r = requests.get(f"{API}/health", timeout=TIMEOUT)
    assert r.status_code == 200
    h = r.headers
    assert "max-age=31536000" in h.get("Strict-Transport-Security", "")
    assert h.get("X-Content-Type-Options") == "nosniff"
    assert h.get("X-Frame-Options") == "DENY"
    assert h.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    assert "geolocation=()" in h.get("Permissions-Policy", "")
    assert "default-src 'none'" in h.get("Content-Security-Policy", "")


# ─── turnstile config endpoint ───────────────────────────────────────
def test_turnstile_config_public_and_no_secret_leak():
    r = requests.get(f"{API}/auth/turnstile-config", timeout=TIMEOUT)
    assert r.status_code == 200
    body = r.json()
    assert {"state", "code", "site_key", "degraded_login"} <= set(body.keys())
    assert body["state"] in ("disabled", "ready", "misconfigured", "provider_degraded", "break_glass")
    secret = os.environ.get("TURNSTILE_SECRET_KEY", "")
    assert secret and secret not in r.text


def test_turnstile_admin_toggle_and_login_gate():
    s = _admin()
    # baseline: disabled
    r = s.get(f"{API}/admin/settings/turnstile", timeout=TIMEOUT)
    assert r.status_code == 200
    assert r.json()["configured"] is True  # keys present in this env
    try:
        r = s.post(f"{API}/admin/settings/turnstile",
                   json={"enabled": True}, timeout=TIMEOUT)
        assert r.status_code == 200 and r.json()["enabled"] is True

        # public config now surfaces the site key
        cfg = requests.get(f"{API}/auth/turnstile-config", timeout=TIMEOUT).json()
        assert cfg["state"] == "ready"
        assert cfg["site_key"] == os.environ["TURNSTILE_SITE_KEY"]

        # login WITHOUT a token → 403 turnstile_required (fail-closed)
        r = requests.post(f"{API}/auth/login",
                          json={"email": ADMIN_EMAIL, "password": ADMIN_PW},
                          timeout=TIMEOUT)
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["code"] == "turnstile_required"

        # login with a GARBAGE token → still 403 (siteverify rejects it)
        r = requests.post(f"{API}/auth/login",
                          json={"email": ADMIN_EMAIL, "password": ADMIN_PW,
                                "turnstile_token": "not-a-real-token"},
                          timeout=TIMEOUT)
        assert r.status_code == 403, r.text

        # register + forgot-password also gated
        r = requests.post(f"{API}/auth/register",
                          json={"email": "turnstile-block@example.com",
                                "password": "Kd5#Zt9mW2xVpR7c",
                                "terms_agreed": True}, timeout=TIMEOUT)
        assert r.status_code == 403
        r = requests.post(f"{API}/auth/forgot-password",
                          json={"email": ADMIN_EMAIL}, timeout=TIMEOUT)
        assert r.status_code == 403
    finally:
        r = s.post(f"{API}/admin/settings/turnstile",
                   json={"enabled": False}, timeout=TIMEOUT)
        assert r.status_code == 200 and r.json()["enabled"] is False

    # disabled again → login works with no token
    r = requests.post(f"{API}/auth/login",
                      json={"email": ADMIN_EMAIL, "password": ADMIN_PW},
                      timeout=TIMEOUT)
    assert r.status_code == 200, r.text


def test_turnstile_toggle_requires_admin():
    from helpers import register_and_login
    s = register_and_login(f"turnstile-user-{os.urandom(4).hex()}@example.com")
    r = s.post(f"{API}/admin/settings/turnstile", json={"enabled": True},
               timeout=TIMEOUT)
    assert r.status_code == 403


# ─── turnstile gate unit behavior ────────────────────────────────────
def test_gate_noop_when_disabled():
    from turnstile_gate import require_turnstile, set_enabled
    db = _db()

    async def go():
        await set_enabled(db, False)
        await require_turnstile(db, None, "1.2.3.4")  # must not raise
    _run(go())


def test_verify_token_missing_is_client_fault():
    from turnstile_gate import verify_token

    async def go():
        return await verify_token("")
    res = _run(go())
    assert res["ok"] is False and res["outage"] is False


# ─── CF-Connecting-IP handling ───────────────────────────────────────
class _StubRequest:
    def __init__(self, headers, host="10.0.0.9"):
        self.headers = headers
        self.client = type("C", (), {"host": host})()


def test_client_ip_ignores_cf_header_by_default(monkeypatch):
    from security import client_ip
    monkeypatch.setenv("TRUST_CF_CONNECTING_IP", "false")
    req = _StubRequest({"cf-connecting-ip": "203.0.113.7",
                        "x-forwarded-for": "6.6.6.6, 198.51.100.1"})
    assert client_ip(req) == "198.51.100.1"  # rightmost XFF, CF ignored


def test_client_ip_uses_cf_header_when_trusted(monkeypatch):
    from security import client_ip
    monkeypatch.setenv("TRUST_CF_CONNECTING_IP", "true")
    req = _StubRequest({"cf-connecting-ip": "203.0.113.7",
                        "x-forwarded-for": "6.6.6.6, 198.51.100.1"})
    assert client_ip(req) == "203.0.113.7"


# ─── session idle timeout ────────────────────────────────────────────
def test_idle_session_is_revoked(monkeypatch):
    from security import consume_and_rotate
    from fastapi import HTTPException
    db = _db()
    monkeypatch.setenv("SESSION_IDLE_TIMEOUT_MINUTES", "60")
    stale = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    jti = f"iter155-{os.urandom(6).hex()}"

    async def go():
        await db.auth_sessions.insert_one({
            "jti": jti, "session_id": "s155", "family": f"fam-{jti}",
            "user_id": "iter155-user", "token_hash": None,
            "created_at": stale, "last_used_at": stale,
            "expires_at": datetime.now(timezone.utc) + timedelta(days=30),
            "revoked": False, "consumed": False})
        with pytest.raises(HTTPException) as e:
            await consume_and_rotate(db, {"jti": jti}, "any-token")
        assert e.value.status_code == 401
        assert e.value.detail["code"] == "session_idle_timeout"
        doc = await db.auth_sessions.find_one({"jti": jti})
        assert doc["revoked"] is True
        await db.auth_sessions.delete_many({"family": f"fam-{jti}"})
    _run(go())


def test_fresh_session_not_idle_revoked(monkeypatch):
    from security import consume_and_rotate
    db = _db()
    monkeypatch.setenv("SESSION_IDLE_TIMEOUT_MINUTES", "60")
    now = datetime.now(timezone.utc).isoformat()
    jti = f"iter155f-{os.urandom(6).hex()}"

    async def go():
        await db.auth_sessions.insert_one({
            "jti": jti, "session_id": "s155f", "family": f"fam-{jti}",
            "user_id": "iter155-user", "token_hash": None,
            "created_at": now, "last_used_at": now,
            "expires_at": datetime.now(timezone.utc) + timedelta(days=30),
            "revoked": False, "consumed": False})
        claims = await consume_and_rotate(db, {"jti": jti}, "any-token")
        assert claims and claims["jti"] != jti
        await db.auth_sessions.delete_many({"family": f"fam-{jti}"})
    _run(go())


# ─── new-IP login alerts ─────────────────────────────────────────────
def test_is_new_ip_logic():
    from login_alerts import is_new_ip
    db = _db()
    uid = f"iter155-alert-{os.urandom(4).hex()}"

    async def go():
        # no history → not "new" (first login is not alert-worthy)
        assert await is_new_ip(db, uid, "9.9.9.9") is False
        await db.auth_sessions.insert_one(
            {"jti": f"a-{uid}", "user_id": uid, "ip": "1.1.1.1",
             "family": f"fam-{uid}", "revoked": False, "consumed": False})
        assert await is_new_ip(db, uid, "9.9.9.9") is True   # unseen IP
        assert await is_new_ip(db, uid, "1.1.1.1") is False  # known IP
        assert await is_new_ip(db, uid, "unknown") is False
        await db.auth_sessions.delete_many({"user_id": uid})
    _run(go())


def test_new_ip_login_writes_notification(monkeypatch):
    import email_sender
    from login_alerts import notify_new_login
    db = _db()
    sent = []

    async def _fake_send(recipient, subject, html, text=None, sender=None):
        sent.append(recipient)
        return {"ok": True, "id": "fake"}
    monkeypatch.setattr(email_sender, "send_email", _fake_send)

    user = {"_id": ObjectId(), "email": "iter155-notify@example.com",
            "name": "Iter155"}

    async def go():
        await notify_new_login(db, user, "203.0.113.9", "TestAgent/1.0")
        doc = await db.notifications.find_one(
            {"user_id": str(user["_id"]), "kind": "security_new_login"})
        assert doc and "203.0.113.9" in doc["message"]
        await db.notifications.delete_many({"user_id": str(user["_id"])})
    _run(go())
    assert sent == ["iter155-notify@example.com"]


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
