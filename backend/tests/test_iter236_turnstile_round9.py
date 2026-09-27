from live_target import ADMIN_EMAIL, ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
"""iter-236 — audit round 9 Turnstile corrections (P1-02..P1-05).

P1-02 strict claim binding · P1-03 typed gate decision + bound degraded OTP ·
P1-04 explicit public state + production config rule · P1-05 governed, durable
break-glass that blocks promotion until post-incident review.
"""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import requests
from fastapi import HTTPException

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)
from dotenv import load_dotenv
load_dotenv(os.path.join(_BACKEND_DIR, ".env"))

from live_target import require_live_base_url
BASE_URL = require_live_base_url()
API = f"{BASE_URL}/api"
TIMEOUT = 25


def _run(coro):
    from conftest import run_async
    return run_async(coro)


def _db():
    from database import get_db
    return get_db()


# ───────────────────────────── P1-02 claim binding ─────────────────────────
class TestClaimBinding:
    def test_missing_and_wrong_claims_fail(self, monkeypatch):
        import turnstile_gate as tg
        monkeypatch.setenv("TURNSTILE_EXPECTED_HOSTNAMES", "app.example.com")
        now = datetime.now(timezone.utc).isoformat()
        cases = {
            "action-missing": {"success": True, "hostname": "app.example.com", "challenge_ts": now},
            "action-mismatch": {"success": True, "action": "register", "hostname": "app.example.com", "challenge_ts": now},
            "hostname-missing": {"success": True, "action": "login", "challenge_ts": now},
            "hostname-mismatch": {"success": True, "action": "login", "hostname": "evil.example", "challenge_ts": now},
            "token-stale": {"success": True, "action": "login", "hostname": "app.example.com",
                            "challenge_ts": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()},
        }
        for code, body in cases.items():
            ok, codes = tg.bind_claims(body, "login")
            assert not ok and code in codes, (code, codes)

    def test_exact_action_and_allowed_hostname_pass(self, monkeypatch):
        import turnstile_gate as tg
        monkeypatch.setenv("TURNSTILE_EXPECTED_HOSTNAMES", "app.example.com, stoic.example")
        ok, codes = tg.bind_claims({"success": True, "action": "login", "hostname": "STOIC.example",
                                    "challenge_ts": datetime.now(timezone.utc).isoformat()}, "login")
        assert ok and codes == []

    def test_no_allowlist_means_hostname_not_required(self, monkeypatch):
        import turnstile_gate as tg
        monkeypatch.delenv("TURNSTILE_EXPECTED_HOSTNAMES", raising=False)
        ok, _ = tg.bind_claims({"success": True, "action": "login",
                                "challenge_ts": datetime.now(timezone.utc).isoformat()}, "login")
        assert ok

    def test_token_cannot_be_replayed_across_surfaces(self, monkeypatch):
        import turnstile_gate as tg
        import turnstile_break_glass as tbg
        monkeypatch.setenv("TURNSTILE_SECRET_KEY", "dummy-secret")
        monkeypatch.setenv("TURNSTILE_SITE_KEY", "dummy-site")
        monkeypatch.delenv("TURNSTILE_FORCE_DISABLE", raising=False)

        async def _enabled(db):
            return True

        async def _no_bg(db):
            return None

        async def _verify(token, ip=None, action=None):
            return {"ok": True, "state": "ok", "outage": False, "error_codes": [], "hostname": None,
                    "action": action, "challenge_ts": None}
        monkeypatch.setattr(tg, "is_enabled", _enabled)
        monkeypatch.setattr(tbg, "active", _no_bg)
        monkeypatch.setattr(tg, "verify_token", _verify)
        token = f"tok-{uuid.uuid4().hex}"
        first = _run(tg.evaluate(_db(), token, "1.2.3.4", action="login"))
        assert first.allow and first.mode == "verified"
        for action in ("register", "password_reset", "login"):
            again = _run(tg.evaluate(_db(), token, "1.2.3.4", action=action))
            assert not again.allow and again.error.status_code == 403
            assert "token-replayed" in again.error_codes, action


# ───────────────────────── P1-03 degraded login OTP ────────────────────────
@pytest.fixture
def degraded(monkeypatch):
    """Provider outage + otp_required policy, real Mongo, captured OTP emails."""
    import turnstile_gate as tg
    import turnstile_break_glass as tbg
    import email_sender
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "dummy-secret")
    monkeypatch.setenv("TURNSTILE_SITE_KEY", "dummy-site")
    monkeypatch.setenv("TURNSTILE_LOGIN_DEGRADED_POLICY", "otp_required")
    monkeypatch.setenv("APP_ENV", "preview")
    monkeypatch.delenv("TURNSTILE_FORCE_DISABLE", raising=False)
    state = {"provider": "provider_unavailable", "sent": []}

    async def _enabled(db):
        return True

    async def _no_bg(db):
        return None

    async def _verify(token, ip=None, action=None):
        ok = state["provider"] == "ok"
        return {"ok": ok, "state": state["provider"], "outage": not ok,
                "error_codes": [] if ok else ["network-error"], "hostname": None, "action": action, "challenge_ts": None}

    async def _send(recipient, subject, html, text=None, sender=None):
        state["sent"].append({"to": recipient, "subject": subject})
        return {"ok": True, "id": "test"}
    monkeypatch.setattr(tg, "is_enabled", _enabled)
    monkeypatch.setattr(tbg, "active", _no_bg)
    monkeypatch.setattr(tg, "verify_token", _verify)
    monkeypatch.setattr(email_sender, "send_email", _send)
    return state


def _login_call(email, password, email_otp=None, ua="pytest-ua", ip="9.9.9.9"):
    from starlette.requests import Request
    from starlette.responses import Response
    from routes.auth_routes import login
    from models import LoginRequest
    scope = {"type": "http", "method": "POST", "path": "/api/auth/login", "query_string": b"",
             "headers": [(b"user-agent", ua.encode()), (b"x-forwarded-for", ip.encode())],
             "client": (ip, 1234), "server": ("test", 80), "scheme": "http"}
    req = Request(scope)
    payload = LoginRequest(email=email, password=password, email_otp=email_otp,
                           turnstile_token=f"tok-{uuid.uuid4().hex}")
    return _run(login(payload, req, Response()))


@pytest.fixture
def degraded_user():
    from auth import hash_password
    email = f"iter236-degraded-{uuid.uuid4().hex[:8]}@example.com"
    pw = f"Vx9#{uuid.uuid4().hex[:12]}Q!"
    db = _db()
    _run(db.users.insert_one({"email": email, "password_hash": hash_password(pw), "name": "d", "role": "user",
                              "status": "active", "email_verified": True, "two_factor_enabled": False,
                              "created_at": datetime.now(timezone.utc).isoformat()}))
    yield email, pw
    _run(db.users.delete_many({"email": email}))
    _run(db.degraded_login_otps.delete_many({}))


class TestDegradedLoginOTP:
    def test_outage_plus_correct_password_enters_otp_challenge(self, degraded, degraded_user):
        email, pw = degraded_user
        with pytest.raises(HTTPException) as e:
            _login_call(email, pw)
        assert e.value.status_code == 401 and e.value.detail["code"] == "turnstile_degraded_otp_sent"
        assert degraded["sent"] and degraded["sent"][-1]["to"] == email
        doc = _run(_db().degraded_login_otps.find_one({"user_id": {"$exists": True}}))
        assert doc and doc["reason"] == "provider_unavailable"

    def test_wrong_password_is_generic_and_issues_no_otp(self, degraded, degraded_user):
        email, _ = degraded_user
        with pytest.raises(HTTPException) as e:
            _login_call(email, "definitely-wrong-password-1!")
        assert e.value.status_code == 401 and e.value.detail == "Invalid email or password"
        assert degraded["sent"] == []
        assert _run(_db().degraded_login_otps.count_documents({})) == 0

    def test_valid_otp_completes_one_login_replay_fails(self, degraded, degraded_user, monkeypatch):
        import degraded_login_otp as dlo
        email, pw = degraded_user
        codes = []
        real = dlo.secrets.randbelow

        def _rand(n):
            v = real(n)
            codes.append(f"{v:06d}")
            return v
        monkeypatch.setattr(dlo.secrets, "randbelow", _rand)
        with pytest.raises(HTTPException):
            _login_call(email, pw)
        code = codes[-1]
        # wrong context (different UA) cannot use the code
        with pytest.raises(HTTPException) as e:
            _login_call(email, pw, email_otp=code, ua="other-device")
        assert e.value.detail["code"] == "turnstile_degraded_otp_invalid"
        out = _login_call(email, pw, email_otp=code)
        assert out["email"] == email
        # replay: code consumed → a NEW challenge is issued, the old code is dead
        with pytest.raises(HTTPException) as e2:
            _login_call(email, pw, email_otp=code)
        assert e2.value.detail["code"] in ("turnstile_degraded_otp_invalid", "turnstile_degraded_otp_expired")

    def test_provider_recovery_returns_to_standard_path(self, degraded, degraded_user):
        email, pw = degraded_user
        degraded["provider"] = "ok"
        out = _login_call(email, pw)
        assert out["email"] == email and degraded["sent"] == []

    def test_registration_and_reset_never_degrade(self, degraded):
        import turnstile_gate as tg
        for action in ("register", "password_reset"):
            with pytest.raises(HTTPException) as e:
                _run(tg.require_turnstile(_db(), "tok", "1.2.3.4", action=action))
            assert e.value.status_code == 503 and e.value.detail["code"] == "turnstile_unavailable"


# ───────────────────────── P1-04 explicit public state ─────────────────────
class TestPublicState:
    def _state(self, monkeypatch, enabled, site="sk", secret="sec"):
        import turnstile_gate as tg
        import turnstile_break_glass as tbg
        monkeypatch.setenv("TURNSTILE_SITE_KEY", site)
        monkeypatch.setenv("TURNSTILE_SECRET_KEY", secret)

        async def _enabled(db):
            return enabled

        async def _no_bg(db):
            return None
        monkeypatch.setattr(tg, "is_enabled", _enabled)
        monkeypatch.setattr(tbg, "active", _no_bg)
        monkeypatch.setattr(tg, "provider_recently_degraded", lambda: False)
        return _run(tg.public_state(_db()))

    def test_enabled_missing_key_is_misconfigured_not_disabled(self, monkeypatch):
        s = self._state(monkeypatch, True, site="")
        assert s["state"] == "misconfigured" and s["site_key"] is None and s["degraded_login"] == "closed"

    def test_disabled_and_ready(self, monkeypatch):
        assert self._state(monkeypatch, False)["state"] == "disabled"
        s = self._state(monkeypatch, True)
        assert s["state"] == "ready" and s["site_key"] == "sk"

    def test_production_refuses_enabled_without_keys(self, monkeypatch):
        import turnstile_gate as tg
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("TURNSTILE_EXPECTED_HOSTNAMES", "app.example.com")
        monkeypatch.setenv("TURNSTILE_SITE_KEY", "")
        monkeypatch.setenv("TURNSTILE_SECRET_KEY", "x")
        assert tg.production_config_violation(True)
        assert tg.production_config_violation(False) is None
        monkeypatch.setenv("TURNSTILE_SITE_KEY", "y")
        assert tg.production_config_violation(True) is None

    def test_live_config_has_explicit_state_and_no_secret(self):
        r = requests.get(f"{API}/auth/turnstile-config", timeout=TIMEOUT)
        assert r.status_code == 200
        body = r.json()
        assert body["state"] in ("disabled", "ready", "misconfigured", "provider_degraded", "break_glass")
        assert "enabled" not in body
        secret = os.environ.get("TURNSTILE_SECRET_KEY", "")
        assert not secret or secret not in r.text


# ───────────────────────── P1-05 governed break-glass ──────────────────────
@pytest.fixture
def bg_clean():
    db = _db()
    saved = _run(db.platform_state.find_one({"_id": "turnstile_break_glass"}))
    _run(db.platform_state.delete_one({"_id": "turnstile_break_glass"}))
    yield db
    _run(db.platform_state.delete_one({"_id": "turnstile_break_glass"}))
    _run(db.turnstile_bypass_events.delete_many({"incident_id": {"$regex": "^INC-236-"}}))
    _run(db.ops_alerts.delete_many({"dedup_key": {"$regex": "^turnstile_break_glass:INC-236-"}}))
    if saved:
        _run(db.platform_state.replace_one({"_id": "turnstile_break_glass"}, saved, upsert=True))


GOOD = {"incident_id": "INC-236-1", "approver": "approver@stoic.test",
        "reason": "Cloudflare widget allowlist broke after domain move; login-only bypass while repaired.",
        "scope": ["login"], "ttl_minutes": 15}


class TestBreakGlassGovernance:
    def test_missing_fields_refuse_activation(self):
        import turnstile_break_glass as tbg
        for missing in ("incident_id", "approver", "reason", "scope"):
            p = {**GOOD, missing: "" if missing != "scope" else []}
            with pytest.raises(HTTPException) as e:
                tbg.validate_request(p, "actor@stoic.test")
            assert e.value.status_code == 400 and e.value.detail["code"] == "break_glass_refused"
        with pytest.raises(HTTPException):
            tbg.validate_request({**GOOD, "ttl_minutes": 240}, "actor@stoic.test")
        with pytest.raises(HTTPException):
            tbg.validate_request(GOOD, "approver@stoic.test")          # approver == actor
        with pytest.raises(HTTPException):
            tbg.validate_request({**GOOD, "scope": ["login", "register"]}, "actor@stoic.test")
        assert tbg.validate_request({**GOOD, "scope": ["login", "register"], "allow_registration_reset": True},
                                    "actor@stoic.test")["scope"] == ["login", "register"]

    def test_lifecycle_persisted_bypass_events_and_promotion_block(self, bg_clean, monkeypatch):
        import turnstile_break_glass as tbg
        import turnstile_gate as tg
        db = bg_clean
        monkeypatch.setenv("TURNSTILE_SECRET_KEY", "dummy-secret")
        monkeypatch.setenv("TURNSTILE_SITE_KEY", "dummy-site")
        monkeypatch.delenv("TURNSTILE_FORCE_DISABLE", raising=False)

        async def _enabled(_db):
            return True
        monkeypatch.setattr(tg, "is_enabled", _enabled)
        assert _run(tbg.readiness_check(db))["ok"] is True

        # round 10 P1-06: direct activation without a signed approver is refused;
        # request → approve by the NAMED approver (≠ actor) is the only path
        with pytest.raises(HTTPException) as e0:
            _run(tbg.activate(db, GOOD, "actor@stoic.test"))
        assert e0.value.detail["code"] == "approval_required"
        req = _run(tbg.request_activation(db, GOOD, "actor@stoic.test"))
        assert req["pending"] and _run(tbg.active(db)) is None
        with pytest.raises(HTTPException) as e1:
            _run(tbg.approve_activation(db, "someone-else@stoic.test"))
        assert e1.value.detail["code"] == "approver_mismatch"
        with pytest.raises(HTTPException) as e2:
            _run(tbg.approve_activation(db, "actor@stoic.test"))
        assert e2.value.status_code == 403
        st = _run(tbg.approve_activation(db, "approver@stoic.test"))
        assert st["active"] and st["promotion_blocked"] and st["record"]["approved_by"] == "approver@stoic.test"
        chain = _run(db.admin_audit_log.find_one({"action": "turnstile_break_glass_activated",
                                                 "meta.incident_id": "INC-236-1"}))
        assert chain and chain.get("entry_hash")
        assert _run(db.ops_alerts.find_one({"dedup_key": "turnstile_break_glass:INC-236-1"}))

        # "restart": a fresh read from Mongo still shows the active record
        assert _run(tbg.active(db))["incident_id"] == "INC-236-1"

        # login (in scope) bypasses with a durable event that stores no token
        d = _run(tg.evaluate(db, "secret-token-value", "5.5.5.5", action="login", request_id="rq-1"))
        assert d.allow and d.mode == "break_glass"
        ev = _run(db.turnstile_bypass_events.find_one({"incident_id": "INC-236-1"}))
        assert ev and ev["action"] == "login" and "secret-token-value" not in str(ev)
        assert "5.5.5.5" not in str(ev)

        # register (out of scope) is NOT bypassed → goes to the verifier
        async def _verify(token, ip=None, action=None):
            return {"ok": False, "state": "client_token_invalid", "outage": False,
                    "error_codes": ["invalid-input-response"], "hostname": None, "action": action, "challenge_ts": None}
        monkeypatch.setattr(tg, "verify_token", _verify)
        d2 = _run(tg.evaluate(db, "x", "5.5.5.5", action="register"))
        assert not d2.allow and d2.error.status_code == 403

        # review refused while active; deactivation leaves it pending review → still blocked
        with pytest.raises(HTTPException) as e:
            _run(tbg.review(db, "reviewer@stoic.test", "post-incident review note long enough"))
        assert e.value.status_code == 409
        st = _run(tbg.deactivate(db, "actor@stoic.test", "repaired"))
        assert not st["active"] and st["pending_review"] and st["promotion_blocked"]
        assert _run(tbg.readiness_check(db))["ok"] is False
        st = _run(tbg.review(db, "reviewer@stoic.test", "Root cause: allowlist; fixed; 1 bypass reviewed."))
        assert not st["promotion_blocked"] and _run(tbg.readiness_check(db))["ok"] is True

    def test_expiry_is_automatic(self, bg_clean):
        import turnstile_break_glass as tbg
        db = bg_clean
        _run(tbg.activate(db, {**GOOD, "incident_id": "INC-236-2", "ttl_minutes": 1}, "actor@stoic.test",
                          approved_by="approver@stoic.test"))
        _run(db.platform_state.update_one({"_id": "turnstile_break_glass"},
                                          {"$set": {"until": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}}))
        assert _run(tbg.active(db)) is None
        st = _run(tbg.status(db))
        assert not st["active"] and st["pending_review"] and st["promotion_blocked"]

    def test_admin_endpoints_are_admin_only(self):
        for method, path in (("get", "/admin/settings/turnstile/break-glass"),
                             ("post", "/admin/settings/turnstile/break-glass"),
                             ("post", "/admin/settings/turnstile/break-glass/review")):
            r = getattr(requests, method)(f"{API}{path}", json={}, timeout=TIMEOUT)
            assert r.status_code in (401, 403), (path, r.status_code)

    def test_release_readiness_exposes_turnstile_gates(self):
        s = requests.Session()
        r = s.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        r = s.get(f"{API}/ops/release-readiness", timeout=TIMEOUT)
        checks = r.json()["checks"]
        assert "turnstile_break_glass" in checks and "turnstile_config" in checks
