"""Security audit round 30 — origin fail-closed (SEC-001), admin-only release identity,
master-admin-only anti-lockout, TRUSTED_PROXY_CIDRS production warning.
Run with DB_NAME="" (pure unit)."""
import asyncio
import inspect
import os
import sys
from unittest.mock import patch

import pytest
from bson import ObjectId
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "unit"))
from fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _user(**kw):
    return {"_id": ObjectId(), "email": "u@x.io", "role": "user", "status": "active", **kw}


def _token_for(auth, u):
    return auth.create_access_token(str(u["_id"]), u["email"], sid="sid-r30")


# ── P3: only the MASTER admin survives suspension/termination ─────────────────
def test_suspended_non_master_admin_is_refused_master_admin_passes():
    import auth
    db = FakeDb()
    master = _user(email="admin@stoicaibot.com", role="admin", status="suspended")
    other = _user(email="second-admin@x.io", role="admin", status="terminated")
    plain = _user(email="user@x.io", status="suspended")
    db.users.rows.extend([master, other, plain])
    for u in (master, other, plain):
        db.auth_sessions.rows.append({"session_id": "sid-r30", "revoked": False, "user_id": str(u["_id"])})
    with patch.dict(os.environ, {"JWT_SECRET": "unit-secret", "ADMIN_EMAIL": "admin@stoicaibot.com"}):
        assert auth.is_master_admin("Admin@StoicAIBot.com") and not auth.is_master_admin("second-admin@x.io")
        assert run(auth.validate_access_token(db, _token_for(auth, master), path="/api/accounts"))["role"] == "admin"
        with pytest.raises(auth.TokenRejected) as e:
            run(auth.validate_access_token(db, _token_for(auth, other), path="/api/accounts"))
        assert e.value.status == 403 and e.value.detail["code"] == "account_terminated"
        with pytest.raises(auth.TokenRejected) as e2:
            run(auth.validate_access_token(db, _token_for(auth, plain), path="/api/accounts"))
        assert e2.value.status == 403 and e2.value.detail["code"] == "account_suspended"
    # the login-route IP-block exemption shares the SAME helper
    from routes import auth_routes
    assert "is_master_admin(email)" in inspect.getsource(auth_routes.login)


# ── SEC-001: WS origin check fails CLOSED when no allowlist resolved ──────────
def _ws_close_code(client, headers):
    try:
        with client.websocket_connect("/api/ws", headers=headers) as ws:
            ws.receive_text()
    except WebSocketDisconnect as e:
        return e.code
    raise AssertionError("socket was not closed")


def test_ws_cross_origin_refused_even_with_empty_allowlist_same_origin_reaches_auth():
    import server
    client = TestClient(server.app)
    with patch.dict(os.environ, {"CORS_ORIGINS": ""}):
        assert _ws_close_code(client, {"origin": "https://evil.example"}) == 4403
        # same-origin (Origin host == Host) is trusted → proceeds to cookie auth → 4401 (no token)
        assert _ws_close_code(client, {"origin": "http://testserver", "host": "testserver"}) == 4401
    with patch.dict(os.environ, {"CORS_ORIGINS": "https://stoicaibot.com"}):
        assert _ws_close_code(client, {"origin": "https://evil.example"}) == 4403
        assert _ws_close_code(client, {"origin": "https://stoicaibot.com"}) == 4401


def test_cors_allowlist_has_no_unfiltered_fallback():
    import server
    from security import _allowed_origins
    src = open(server.__file__, encoding="utf-8").read()
    assert "cors_origins_env.split" not in src
    assert server._credentialed_origins == sorted(_allowed_origins())


# ── P3: release identity is admin-only; the public probe stays minimal ────────
def test_release_identity_admin_only_public_health_minimal():
    import server

    class _Db:
        async def command(self, *_a):
            return {"ok": 1}

    client = TestClient(server.app)
    with patch.object(server, "get_db", lambda: _Db()):
        body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert "release_identity" not in body
    assert "image_digest" not in str(body) and "ea_accepted_sha256s" not in str(body)
    assert client.get("/api/health/release").status_code == 401
    assert "require_admin(user)" in inspect.getsource(server.health_release)


# ── P3: TRUSTED_PROXY_CIDRS unset in production → explicit boot warning ──────
def test_production_boot_warns_when_trusted_proxy_cidrs_unset():
    import server
    src = inspect.getsource(server.on_startup)
    assert "TRUSTED_PROXY_CIDRS" in src and "warning(" in src
    assert "TRUSTED_PROXY_CIDRS" in open(os.path.join(os.path.dirname(server.__file__), "..", "deploy", "env", "backend.env.example")).read()


# ── CI: every workflow step that SIGNS via release_signing must install `requests` ─
def test_signing_workflow_steps_install_requests():
    import re
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..")
    for wf in ("ea-release.yml", "release.yml"):
        text = open(os.path.join(root, ".github", "workflows", wf), encoding="utf-8").read()
        for step in re.split(r"\n\s+- name:", text):
            if "verify_ea_release.py" in step and "--sign" in step:
                assert re.search(r"pip install[^\n]*\brequests\b", step), f"{wf}: --sign step lacks `requests` (release_signing._external_sign)"
