"""Live HTTP suite for main94 review (iteration 230) — step A10 corrections.

Covers:
 - GET /api/admin/execution-brakes auth matrix + release flow
 - POST /api/accounts (live) returns plaintext bridge_token ONCE +
   bridge_token_shown_once=True; GET list has no plaintext; PAPER returns none
 - heartbeat with the shown-once token authenticates
 - GET /api/health has no release_identity; /api/health/release 401/non-admin 403/admin 200
 - P1-02 WS re-validation: socket closes with 4401 within ~45s after /auth/logout

NEVER embeds credentials — reads TEST_ADMIN_PASSWORD / STEP_UP_BYPASS_TOKEN /
RATE_LIMIT_BYPASS_TOKEN from the environment (backend/.env is loaded by
conftest.py).  Mirror of test_main93_live_iter229.py conventions.
"""
import asyncio
import json
import os
import ssl
import time
import uuid
from datetime import datetime, timezone

import bcrypt
import pytest
import requests
from bson import ObjectId
from pymongo import MongoClient

pytestmark = pytest.mark.http

BASE = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
STEP_UP = os.environ.get("STEP_UP_BYPASS_TOKEN") or ""
MONGO = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB = os.environ.get("DB_NAME", "ai_trading_bot")

ADMIN_EMAIL = os.environ.get("TEST_ADMIN_EMAIL") or os.environ.get("ADMIN_EMAIL") or "admin@stoicaibot.com"
_P = os.environ.get("TEST_ADMIN_PASSWORD") or ""
if not (BASE and _P and STEP_UP):
    pytest.skip("REACT_APP_BACKEND_URL / TEST_ADMIN_PASSWORD / STEP_UP_BYPASS_TOKEN missing",
                allow_module_level=True)


def _login(email, password):
    s = requests.Session()
    r = s.post(f"{BASE}/api/auth/login", json={"email": email, "password": password}, timeout=20)
    assert r.status_code == 200, (r.status_code, r.text[:300])
    return s


def _mongo():
    return MongoClient(MONGO)[DB]


@pytest.fixture(scope="module")
def admin():
    return _login(ADMIN_EMAIL, _P)


@pytest.fixture(scope="module")
def mongo():
    return _mongo()


# ───────────────────── throwaway user ────────────────────────────────
def _throwaway():
    email = f"test_m94_{uuid.uuid4().hex[:8]}@example.com"
    pw = "Pw0rd!" + uuid.uuid4().hex[:12] + "Az"
    pw_hash = bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()
    uid = str(uuid.uuid4())
    _mongo().users.insert_one({
        "id": uid, "email": email, "password_hash": pw_hash, "name": "QA m94",
        "role": "user", "status": "active",
        "must_change_password": False, "email_verified": True, "two_factor_enabled": False,
    })
    s = _login(email, pw)
    return s, email, pw


# ─────────────────────────────── health ──────────────────────────────
def test_health_has_no_release_identity_and_admin_release_shows_ea_1_59(admin):
    r = requests.get(f"{BASE}/api/health", timeout=15)
    assert r.status_code == 200, r.text
    j = r.json()
    assert "release_identity" not in j, j
    # /api/health/release — 401 unauth
    assert requests.get(f"{BASE}/api/health/release", timeout=15).status_code == 401
    # non-admin → 403
    user_s, user_email, _ = _throwaway()
    try:
        r2 = user_s.get(f"{BASE}/api/health/release", timeout=15)
        assert r2.status_code == 403, (r2.status_code, r2.text[:200])
    finally:
        _mongo().users.delete_one({"email": user_email})
    # admin → 200 + ea_shipped_version 1.60
    r3 = admin.get(f"{BASE}/api/health/release", timeout=15)
    assert r3.status_code == 200, r3.text
    ri = r3.json().get("release_identity") or {}
    assert ri.get("ea_shipped_version") == "1.60", ri


# ───────────────────── admin execution-brakes ────────────────────────
def test_admin_execution_brakes_auth_matrix_and_release(admin, mongo):
    # 401 unauthenticated
    assert requests.get(f"{BASE}/api/admin/execution-brakes", timeout=15).status_code == 401
    # 403 for a normal user
    user_s, user_email, _ = _throwaway()
    try:
        r = user_s.get(f"{BASE}/api/admin/execution-brakes", timeout=15)
        assert r.status_code == 403, (r.status_code, r.text[:200])
        # admin 200
        r2 = admin.get(f"{BASE}/api/admin/execution-brakes", timeout=15)
        assert r2.status_code == 200, r2.text
        assert "accounts" in r2.json()

        # Seed a brake on a throwaway account
        user_doc = mongo.users.find_one({"email": user_email})
        # braked_accounts joins accounts.user_id -> users._id (ObjectId string)
        uid = str(user_doc["_id"])
        oid = ObjectId()
        label = f"TEST_br_{uuid.uuid4().hex[:6]}"
        now_iso = datetime.now(timezone.utc).isoformat()
        mongo.accounts.insert_one({
            "_id": oid, "user_id": uid, "label": label, "mode": "live",
            "broker": "MT5", "server": "TestServer", "account_number": str(90000000 + (uuid.uuid4().int % 9000000)),
            "account_type": "standard", "account_role": "STANDARD", "base_currency": "USD",
            "trading_enabled": False, "status": "connected", "balance": 0, "equity": 0,
            "created_at": now_iso,
            "execution_brake": {"active": True, "since": now_iso,
                                 "reason": "2 late fills in 24 h", "engaged_by": "auto"},
        })
        try:
            r3 = admin.get(f"{BASE}/api/admin/execution-brakes", timeout=15)
            assert r3.status_code == 200
            rows = r3.json().get("accounts", [])
            match = next((a for a in rows if a.get("label") == label), None)
            assert match is not None, f"seeded brake {label} not in admin list: {[a.get('label') for a in rows]}"
            assert match.get("owner_email") == user_email
            assert match.get("execution_brake", {}).get("active") is True

            # Release as admin (step-up auto-bypassed by conftest)
            rr = admin.post(f"{BASE}/api/accounts/{str(oid)}/execution-brake/release", timeout=15)
            assert rr.status_code in (200, 204), (rr.status_code, rr.text[:300])

            # Row should now disappear from admin list
            r4 = admin.get(f"{BASE}/api/admin/execution-brakes", timeout=15)
            assert r4.status_code == 200
            rows2 = r4.json().get("accounts", [])
            assert all(a.get("label") != label for a in rows2), \
                f"brake {label} still present after release"
        finally:
            mongo.accounts.delete_one({"_id": oid})
    finally:
        mongo.users.delete_one({"email": user_email})


# ─────────────── token shown once at account creation ────────────────
@pytest.fixture
def throwaway_user():
    s, email, pw = _throwaway()
    yield s, email, pw
    db = _mongo()
    u = db.users.find_one({"email": email})
    if u:
        uid = u.get("id") or str(u.get("_id"))
        db.accounts.delete_many({"user_id": uid})
        db.users.delete_one({"email": email})


def _mkpayload(mode, label=None):
    return {
        "label": label or f"TEST_m94_{uuid.uuid4().hex[:6]}",
        "broker": "MT5", "server": "TestServer",
        "account_number": str(90000000 + (uuid.uuid4().int % 9000000)),
        "account_type": "standard", "account_role": "STANDARD",
        "base_currency": "USD", "mode": mode,
        "terms_accepted": True,
    }


def test_live_account_creation_returns_token_once_and_list_hides_it(throwaway_user):
    s, email, _pw = throwaway_user
    r = s.post(f"{BASE}/api/accounts", json=_mkpayload("live"), timeout=20)
    assert r.status_code in (200, 201), r.text
    body = r.json()
    acc_id = body.get("id")
    assert acc_id, body
    assert body.get("bridge_token_shown_once") is True, body
    plaintext = body.get("bridge_token")
    assert plaintext and isinstance(plaintext, str) and len(plaintext) > 10, body

    # Subsequent GET /api/accounts must NOT contain plaintext bridge_token
    rl = s.get(f"{BASE}/api/accounts", timeout=15)
    assert rl.status_code == 200
    accs = rl.json() if isinstance(rl.json(), list) else rl.json().get("accounts", [])
    this = next((a for a in accs if a.get("id") == acc_id), None)
    assert this is not None
    assert "bridge_token" not in this, this
    assert "bridge_token_hash" not in this, this
    assert this.get("has_bridge_token") is True
    assert "bridge_token_masked" in this

    # heartbeat using the shown-once token authenticates
    hb = requests.post(
        f"{BASE}/api/bridge/heartbeat",
        json={"bridge_token": plaintext, "ea_version": "1.60",
              "account_login": this.get("account_number"),
              "balance": 0.0, "equity": 0.0, "open_positions": 0},
        timeout=15)
    assert hb.status_code == 200, (hb.status_code, hb.text[:300])


def test_paper_account_creation_does_not_return_bridge_token(throwaway_user):
    s, email, _pw = throwaway_user
    r = s.post(f"{BASE}/api/accounts", json=_mkpayload("paper"), timeout=20)
    assert r.status_code in (200, 201), r.text
    body = r.json()
    assert "bridge_token" not in body, body
    assert body.get("bridge_token_shown_once") is not True, body


# ────────────────────── P1-02 WS re-validation ───────────────────────
async def _ws_run(cookie_header: str, host: str):
    import websockets
    url = f"wss://{host}/api/ws"
    ssl_ctx = ssl.create_default_context()
    extra = {"Cookie": cookie_header, "Origin": f"https://{host}"}
    # websockets 16.x uses additional_headers (10.x used extra_headers).
    try:
        ws = await websockets.connect(url, additional_headers=extra, ssl=ssl_ctx, ping_interval=None)
    except TypeError:
        ws = await websockets.connect(url, extra_headers=extra, ssl=ssl_ctx, ping_interval=None)
    try:
        first = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
        return ws, first
    except Exception:
        await ws.close()
        raise


def test_ws_closes_with_4401_after_logout(throwaway_user):
    s, email, _pw = throwaway_user
    host = BASE.split("://", 1)[-1].rstrip("/")
    cookie_header = "; ".join([f"{c.name}={c.value}" for c in s.cookies])

    async def run():
        ws, hello = await _ws_run(cookie_header, host)
        assert hello.get("type") == "connected", hello
        # Logout now — server must revoke the session and close the socket on next revalidation
        # Reuse the same Session so CSRF header is sent (conftest auto-injects).
        rlog = s.post(f"{BASE}/api/auth/logout", timeout=15)
        assert rlog.status_code in (200, 204), (rlog.status_code, rlog.text[:200])
        t0 = time.monotonic()
        close_code = None
        try:
            # Wait up to 60s — server re-validates every WS_REVALIDATE_SECONDS=30
            while time.monotonic() - t0 < 60:
                try:
                    await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    continue
                except Exception:
                    break
        finally:
            close_code = ws.close_code
            try:
                await ws.close()
            except Exception:
                pass
        elapsed = time.monotonic() - t0
        assert close_code == 4401, f"expected 4401, got {close_code} after {elapsed:.1f}s"
        assert elapsed < 55, f"close took {elapsed:.1f}s (expected <~45s)"

    asyncio.new_event_loop().run_until_complete(run())
