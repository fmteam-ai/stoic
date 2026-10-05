"""Fix plan steps D1 + D2 — logs/small fixes and 2FA/passkey/admin hardening.
D1 S4 URL/query secrets redacted (Telegram bot path, apiKey/api_key/token params); httpx/httpcore at WARNING.
D1 S5 Telegram webhook secret moves to the secret_token header; URL path is the literal `hook`.
D1 S8 production refuses to start without RESEND_API_KEY; activation/reset links never logged, dev-only field only outside production.
D1 S9 bug routes: admin by role only.
D2 S1 passkey enrol/complete/delete require step-up; WEBAUTHN_ORIGIN pinned wins; production refuses unpinned.
D2 S2 /2fa/enroll requires the current password (+ lockout); enable sends an email notice.
D2 S6 ops/authority/infra admin checks require admin 2FA; forced promotion is a step-up action.
D2 S10 API keys revoked on password change/reset/suspension; suspended owners' keys refused.
D2 S11 VPS rebuild/delete require step-up.
Pure unit tests — run with DB_NAME="".
"""
import inspect
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
pytestmark = pytest.mark.unit


def src(rel):
    return open(os.path.join(ROOT, rel)).read()


def test_s4_url_secrets_redacted_and_http_clients_quiet():
    from security_agent.redact import mask, install_log_filter
    assert mask("POST https://api.telegram.org/bot1234567890:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/sendMessage") == "POST https://api.telegram.org/bot[REDACTED]/sendMessage"
    assert mask("GET https://newsapi.org/v2/everything?q=gold&apiKey=0123456789abcdef0123456789abcdef") == "GET https://newsapi.org/v2/everything?q=gold&apiKey=[REDACTED]"
    assert mask("series?series_id=DGS10&api_key=abcdef1234567890abcdef&file_type=json") == "series?series_id=DGS10&api_key=[REDACTED]&file_type=json"
    assert mask("POST /api/telegram/incoming/aBcDeFgHiJkLmNoPqRsTuVwXyZ012345 200") == "POST /api/telegram/incoming/[REDACTED] 200"
    assert mask("GET /activate/0xZt9mW2xVpR7cKd5Zt9mW2xVpR7c") == "GET /activate/[REDACTED]"
    assert mask("plain line: key and /api/bot/status ok") == "plain line: key and /api/bot/status ok"
    install_log_filter()
    for n in ("httpx", "httpcore"):
        assert logging.getLogger(n).level >= logging.WARNING


def test_s5_webhook_secret_in_header_not_path():
    s = src("routes/telegram_routes.py")
    assert 'webhook_url = f"{base}/api/telegram/incoming/hook"' in s
    assert '"secret_token": new_secret' in s
    assert 'request.headers.get("x-telegram-bot-api-secret-token")' in s
    assert 'if not user_doc and secret != "hook":' in s                        # legacy path secrets still accepted until re-enabled


def test_s8_links_never_logged_and_production_requires_resend(monkeypatch):
    a, pr, sv = src("activation.py"), src("password_reset.py"), src("server.py")
    assert "activation link for %s: %s" not in a and "reset link for %s: %s" not in pr
    assert 'if not is_production():\n            out["activation_link_dev_only"] = link' in a
    assert 'if not is_production():\n            out["reset_link_dev_only"] = link' in pr
    assert 'APP_ENV=production requires RESEND_API_KEY' in sv
    import activation, app_env
    monkeypatch.setattr(activation, "email_is_configured", lambda: False)
    monkeypatch.setattr(app_env, "is_production", lambda: True)
    import asyncio
    out = asyncio.new_event_loop().run_until_complete(activation.send_activation_email(recipient="x@y.z", name="x", token="tok" * 8))
    assert out == {"ok": False, "error": "email_not_configured"}
    monkeypatch.setattr(app_env, "is_production", lambda: False)
    out = asyncio.new_event_loop().run_until_complete(activation.send_activation_email(recipient="x@y.z", name="x", token="tok" * 8))
    assert "activation_link_dev_only" in out


def test_s9_bug_routes_admin_by_role_only():
    from routes.bugs_routes import _is_admin
    assert _is_admin({"role": "admin", "email": "x@y.z"}) and not _is_admin({"role": "user", "email": "admin@trading.bot"})
    assert "admin@" not in inspect.getsource(_is_admin)


def test_s1_passkeys_need_step_up_and_pinned_origin(monkeypatch):
    import routes.webauthn_routes as wr
    for fn in (wr.register_begin, wr.register_complete, wr.delete_passkey):
        assert 'require_step_up(' in inspect.getsource(fn) and '"passkey_enrol"' in inspect.getsource(fn)
    from step_up import STEP_UP_ACTIONS
    assert {"passkey_enrol", "admin_promote", "vps_destroy", "security_finding_status", "security_agent_mode",
            "security_test_alert", "security_action_undo", "security_action_extend"} <= STEP_UP_ACTIONS

    class Req:
        headers = {"origin": "https://evil.example"}
    monkeypatch.setenv("WEBAUTHN_ORIGIN", "https://stoicaibot.com")
    assert wr._origin(Req(), {"origin": "https://evil.example"}) == "https://stoicaibot.com"     # pinned wins over client/header
    monkeypatch.delenv("WEBAUTHN_ORIGIN")
    import app_env
    monkeypatch.setattr(app_env, "is_production", lambda: True)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        wr._origin(Req(), None)
    assert ei.value.status_code == 503
    monkeypatch.setattr(app_env, "is_production", lambda: False)
    assert wr._origin(Req(), None) == "https://evil.example"                                       # dev: header fallback (RP-scoped)


def test_s2_2fa_enrol_requires_password_and_notifies():
    import routes.auth_routes as ar
    from models import TOTPEnrollRequest
    s = inspect.getsource(ar.two_fa_enroll)
    assert "payload: TOTPEnrollRequest" in s and 'verify_password(payload.current_password' in s and '"2fa_enroll"' in s
    assert "Two-factor authentication enabled" in inspect.getsource(ar.two_fa_verify_enroll)
    with pytest.raises(Exception):
        TOTPEnrollRequest(current_password="")
    fe = src("../frontend/src/pages/Settings.jsx")
    assert 'api.post("/auth/2fa/enroll", { current_password: enrollPw })' in fe and 'data-testid="enable-2fa-password"' in fe


def test_s6_admin_checks_require_mfa_and_forced_promotion_steps_up(monkeypatch):
    import routes.ops_routes as ops
    monkeypatch.setenv("ADMIN_MFA_ENFORCED", "true")
    assert ops._admin_ok({"role": "admin", "two_factor_enabled": True}) is True
    assert ops._admin_ok({"role": "admin", "two_factor_enabled": False}) is False
    assert ops._admin_ok({"role": "user", "two_factor_enabled": True}) is False and ops._admin_ok(None) is False
    for rel in ("routes/ops_routes.py", "routes/authority_routes.py", "routes/infra_routes.py"):
        body = src(rel).replace('if not u or u.get("role") != "admin":', "")      # the helper's own role test
        assert 'role") == "admin"' not in body and 'role") != "admin"' not in body, rel
    vs = src("routes/validation_routes.py")
    assert 'require_step_up(db, admin_user, request, "admin_promote")' in vs and "forced promotion requires an admin session" in vs


def test_s10_api_keys_die_with_credentials_and_suspension():
    from security import revoke_user_api_keys
    import asyncio
    sys.path.insert(0, os.path.join(ROOT, "tests", "unit"))
    from fake_mongo import FakeDb
    db = FakeDb()
    db.api_keys.rows += [{"_id": 1, "user_id": "u1", "revoked_at": None}, {"_id": 2, "user_id": "u1", "revoked_at": "x"}, {"_id": 3, "user_id": "u2", "revoked_at": None}]
    assert asyncio.new_event_loop().run_until_complete(revoke_user_api_keys(db, "u1", "password_change")) == 1
    assert db.api_keys.rows[0]["revoked_at"] and db.api_keys.rows[0]["revoked_reason"] == "password_change" and db.api_keys.rows[2]["revoked_at"] is None
    ar, adm, ent = src("routes/auth_routes.py"), src("routes/admin_routes.py"), src("routes/enterprise_routes.py")
    assert ar.count("revoke_user_api_keys(") == 2 and '"password_reset"' in ar and '"password_change"' in ar
    assert 'revoke_user_api_keys(db, user_id, "account_suspended")' in adm
    assert 'key owner account suspended' in ent


def test_s11_vps_destructive_actions_step_up():
    import routes.infra_routes as ir
    s = inspect.getsource(ir.server_action)
    assert 'in ("rebuild", "delete")' in s and 'require_step_up(get_db(), user, request, "vps_destroy")' in s
