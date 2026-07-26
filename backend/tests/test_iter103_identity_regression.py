"""iter-103 (test agent) — Regression HTTP+WS smoke for iter-125 identity
corrections review request. Runs the exact scenarios listed in the review:

  · admin login + /api/auth/me cookie session
  · WS /api/ws — accepted with cookie (connected frame), 4401 without
  · /api/infra/artifacts/manifest — 200 with non-null signature.value
  · /api/ea-script.ex5 — 409 ex5_not_published (no CI binary in preview)
  · POST /api/infra/agent/artifact-digest — 401 no-token, 200 match=false
    with valid bridge_token + wrong sha
  · Heartbeat without installation_id — 200 telemetry, NO lease renewed
  · Account create stamps display_name + expected_identity
  · /api/ea-script contains v1.55 markers
  · /api/setup/installer.ps1 contains STOIC-Installation + digest report
  · Live-trade identity gate: blocked=identity without ea_identity, gate
    passes once installation + lease + verified ea_identity are seeded.
"""
from __future__ import annotations

import os
import re
import ssl
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import requests
from bson import ObjectId
from pymongo import MongoClient

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROOT_DIR = os.path.dirname(_BACKEND_DIR)

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    with open(os.path.join(_ROOT_DIR, "frontend", ".env")) as f:
        for ln in f:
            if ln.startswith("REACT_APP_BACKEND_URL"):
                BASE_URL = ln.split("=", 1)[1].strip().strip('"').rstrip("/")

TIMEOUT = 30


def _mongo():
    with open(os.path.join(_BACKEND_DIR, ".env")) as f:
        cfg = {ln.split("=", 1)[0]: ln.split("=", 1)[1].strip().strip("\"'")
               for ln in f if "=" in ln and not ln.startswith("#")}
    return MongoClient(cfg["MONGO_URL"])[cfg["DB_NAME"]]


# ─── fixtures ─────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": "admin@trading.bot", "password": "admin123"},
               timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    # attach CSRF header for mutating requests
    csrf = s.cookies.get("csrf_token")
    if csrf:
        s.headers.update({"X-CSRF-Token": csrf})
    return s


@pytest.fixture
def fresh_user():
    suffix = uuid.uuid4().hex[:10]
    email = f"iter103_{suffix}@example.com"
    pw = "Gy6#Vb3kM9zRnD2s"
    r = requests.post(f"{BASE_URL}/api/auth/register",
                      json={"email": email, "password": pw,
                            "name": f"iter103-{suffix}",
                            "terms_agreed": True}, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    uid = r.json()["id"]
    _mongo().users.update_one(
        {"_id": ObjectId(uid)},
        {"$set": {"email_verified": True},
         "$unset": {"activation_token": "",
                    "activation_expires_at": ""}})
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": email, "password": pw}, timeout=TIMEOUT)
    assert r.status_code == 200
    csrf = s.cookies.get("csrf_token")
    if csrf:
        s.headers.update({"X-CSRF-Token": csrf})
    # Grant elite subscription so the test-trade path exercises identity gate
    valid = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    _mongo().subscriptions.update_one(
        {"user_id": uid},
        {"$set": {"current_plan_id": "elite_ai_monthly",
                  "valid_until": valid}},
        upsert=True)
    yield uid, email, s
    _mongo().users.delete_one({"_id": ObjectId(uid)})
    _mongo().accounts.delete_many({"user_id": uid})
    _mongo().subscriptions.delete_many({"user_id": uid})


# ─── 1) admin login + /me ────────────────────────────────────────
def test_admin_login_and_me(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/auth/me", timeout=TIMEOUT)
    assert r.status_code == 200
    assert r.json().get("email") == "admin@trading.bot"


# ─── 2) WebSocket smoke: with cookie → connected; no cookie → 4401 ─
def test_ws_connected_with_cookie(admin_session):
    from websockets.sync.client import connect  # noqa: WPS433

    ws_url = BASE_URL.replace("https://", "wss://").replace(
        "http://", "ws://") + "/api/ws"
    access = admin_session.cookies.get("access_token")
    assert access, "expected access_token cookie after admin login"
    ssl_ctx = ssl.create_default_context()
    headers = [("Cookie", f"access_token={access}")]
    with connect(ws_url, additional_headers=headers,
                 ssl_context=ssl_ctx, open_timeout=15) as ws:
        msg = ws.recv(timeout=15)
        import json as _json
        payload = _json.loads(msg)
        assert payload.get("type") == "connected"


def test_ws_rejects_without_cookie():
    from websockets.sync.client import connect
    from websockets.exceptions import ConnectionClosed

    ws_url = BASE_URL.replace("https://", "wss://").replace(
        "http://", "ws://") + "/api/ws"
    ssl_ctx = ssl.create_default_context()
    try:
        with connect(ws_url, ssl_context=ssl_ctx, open_timeout=15) as ws:
            # server accepts then closes with 4401; recv should raise
            with pytest.raises(ConnectionClosed) as exc:
                ws.recv(timeout=10)
            assert exc.value.code == 4401
    except Exception as e:
        # If connect itself failed with an HTTP 403 the fix is broken
        pytest.fail(f"WS handshake did not accept-then-close: {e!r}")


# ─── 3) artifacts manifest ───────────────────────────────────────
def test_artifacts_manifest_signed():
    r = requests.get(f"{BASE_URL}/api/infra/artifacts/manifest",
                     timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    body = r.json()
    sig = body.get("signature") or {}
    assert sig.get("value"), "manifest signature.value must be non-null"
    names = {a["name"]: a for a in body.get("artifacts", [])}
    assert "stoic-ea" in names
    ea_url = names["stoic-ea"]["url"]
    assert (ea_url == "/api/ea-script"
            or re.fullmatch(r"/api/artifacts/[0-9a-f]{64}", ea_url)), ea_url
    assert names["stoic-ea"].get("version") == "1.55"
    assert "stoic-ea-ex5" in names
    ex5_url = names["stoic-ea-ex5"]["url"]
    assert (ex5_url == "/api/ea-script.ex5"
            or re.fullmatch(r"/api/artifacts/[0-9a-f]{64}", ex5_url)), ex5_url


# ─── 4) /api/ea-script.ex5 → 409 ex5_not_published in preview ────
def test_ex5_not_published_in_preview():
    r = requests.get(f"{BASE_URL}/api/ea-script.ex5",
                     timeout=TIMEOUT, allow_redirects=True)
    assert r.status_code == 409, f"unexpected {r.status_code}: {r.text[:200]}"
    assert r.json().get("error") == "ex5_not_published"


# ─── 5) artifact-digest auth ─────────────────────────────────────
def test_artifact_digest_requires_token():
    r = requests.post(f"{BASE_URL}/api/infra/agent/artifact-digest",
                      json={"artifact": "stoic-ea-ex5",
                            "sha256": "ab" * 32}, timeout=TIMEOUT)
    assert r.status_code == 401


def test_artifact_digest_reports_mismatch(fresh_user):
    uid, email, sess = fresh_user
    # create an account so we have a bridge_token
    r = sess.post(f"{BASE_URL}/api/accounts", json={
        "label": f"iter103-{uuid.uuid4().hex[:6]}", "broker": "STARTRADER",
        "server": "T-Demo",
        "account_number": "iter103-" + uuid.uuid4().hex[:8],
        "account_type": "demo", "base_currency": "USD", "mode": "live",
    }, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    aid = r.json()["id"]
    acc = _mongo().accounts.find_one({"_id": ObjectId(aid)})
    r2 = requests.post(f"{BASE_URL}/api/infra/agent/artifact-digest",
                       json={"bridge_token": acc["bridge_token"],
                             "artifact": "stoic-ea",
                             "sha256": "de" * 32, "version": "1.55"},
                       timeout=TIMEOUT)
    assert r2.status_code == 200, r2.text
    assert r2.json().get("match") is False


# ─── 6) heartbeat without installation_id doesn't renew lease ────
def test_heartbeat_no_installation_id_never_renews_lease(fresh_user):
    uid, email, sess = fresh_user
    r = sess.post(f"{BASE_URL}/api/accounts", json={
        "label": f"iter103hb-{uuid.uuid4().hex[:6]}", "broker": "STARTRADER",
        "server": "T-Demo",
        "account_number": "iter103-hb-" + uuid.uuid4().hex[:8],
        "account_type": "demo", "base_currency": "USD", "mode": "live",
    }, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    aid = r.json()["id"]
    acc = _mongo().accounts.find_one({"_id": ObjectId(aid)})
    # ensure no pre-existing execution_leases doc for this account
    _mongo().execution_leases.delete_many({"account_id": aid})
    # Post a legacy-style heartbeat WITHOUT installation_id
    hb = requests.post(f"{BASE_URL}/api/bridge/heartbeat",
                       json={"bridge_token": acc["bridge_token"],
                             "account_login": "999001",
                             "balance": 10000, "equity": 10000,
                             "available_symbols": ["BTCUSD"]},
                       timeout=TIMEOUT)
    assert hb.status_code == 200, hb.text
    # Give MongoDB a beat then verify no lease was created/renewed
    lease = _mongo().execution_leases.find_one({"account_id": aid})
    assert lease is None or lease.get("expires_at") is None or (
        lease["expires_at"].replace(tzinfo=timezone.utc)
        if lease["expires_at"].tzinfo is None else lease["expires_at"]
    ) <= datetime.now(timezone.utc), (
        "unidentified heartbeat must not create/renew execution lease")


# ─── 7) account create stamps display_name + expected_identity ──
def test_account_create_stamps_identity_structure(fresh_user):
    uid, email, sess = fresh_user
    label = f"iter103id-{uuid.uuid4().hex[:6]}"
    server = "ICMarketsSC-Live"
    number = "id-" + uuid.uuid4().hex[:8]
    r = sess.post(f"{BASE_URL}/api/accounts", json={
        "label": label, "broker": "IC Markets", "server": server,
        "account_number": number,
        "account_type": "demo", "base_currency": "USD", "mode": "live",
    }, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    aid = r.json()["id"]
    acc = _mongo().accounts.find_one({"_id": ObjectId(aid)})
    assert acc.get("display_name") == label
    ei = acc.get("expected_identity") or {}
    assert ei.get("account_number") == number
    assert ei.get("broker_server") == server


# ─── 8) /api/ea-script contains v1.55 identity markers ──────────
def test_ea_script_has_v155_markers():
    r = requests.get(f"{BASE_URL}/api/ea-script", timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    src = r.text
    assert '#define EA_CLIENT_VERSION "1.55"' in src or \
        'EA_CLIENT_VERSION "1.55"' in src
    assert "installation_id" in src
    assert "ResolveInstallationId" in src
    assert "STOIC-Installation.txt" in src


# ─── 9) /api/setup/installer.ps1 contains STOIC + digest logic ──
def test_installer_script_has_identity_and_digest_logic():
    r = requests.get(f"{BASE_URL}/api/setup/installer.ps1", timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    src = r.text
    assert "STOIC-Installation.txt" in src
    assert "/api/ea-script.ex5" in src
    assert "X-STOIC-SHA256" in src
    assert "artifact-digest" in src


# ─── 10) live-trade identity gate blocks then unblocks ──────────
def test_live_test_trade_identity_gate(fresh_user):
    uid, email, sess = fresh_user
    r = sess.post(f"{BASE_URL}/api/accounts", json={
        "label": f"iter103gate-{uuid.uuid4().hex[:6]}",
        "broker": "STARTRADER", "server": "T-Demo",
        "account_number": "iter103-gate-" + uuid.uuid4().hex[:8],
        "account_type": "demo", "base_currency": "USD", "mode": "live",
    }, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    aid = r.json()["id"]
    now = datetime.now(timezone.utc)
    # Seed healthy heartbeat + tradeable symbol; INTENTIONALLY no ea_identity
    _mongo().accounts.update_one({"_id": ObjectId(aid)}, {"$set": {
        "status": "connected", "last_heartbeat": now.isoformat(),
        "ea_version": "1.55",
        "available_symbols": ["BTCUSD", "XAUUSD", "EURUSD"],
        "balance": 10000.0, "equity": 10000.0}})
    r = sess.post(f"{BASE_URL}/api/accounts/{aid}/test-trade",
                  timeout=TIMEOUT)
    assert r.status_code == 409, r.text
    body = r.json().get("detail") or {}
    # The route surfaces result["blocked"] as detail.code, with context=result
    assert body.get("code") == "identity" or (
        (body.get("context") or {}).get("blocked") == "identity"), body

    # Now seed a verified installation + owned unexpired lease
    inst_id = f"inst_iter103_{uuid.uuid4().hex[:8]}"
    _mongo().accounts.update_one({"_id": ObjectId(aid)}, {"$set": {
        "ea_identity": {"installation_id": inst_id, "authoritative": True,
                        "ea_version": "1.55",
                        "verified_at": now.isoformat()}}})
    _mongo().installations.update_one(
        {"installation_id": inst_id},
        {"$set": {"installation_id": inst_id, "user_id": uid,
                  "account_id": aid, "terminal_path": "C:/mt5",
                  "host_fingerprint": "iter103", "revoked": False,
                  "created_at": now}}, upsert=True)
    _mongo().execution_leases.update_one(
        {"account_id": aid},
        {"$set": {"installation_id": inst_id, "user_id": uid,
                  "revoked": False,
                  "expires_at": now + timedelta(seconds=120)}}, upsert=True)
    r2 = sess.post(f"{BASE_URL}/api/accounts/{aid}/test-trade",
                   timeout=TIMEOUT)
    # It may still block for downstream reasons (e.g. no live EA), but the
    # block reason MUST NO LONGER be 'identity'.
    if r2.status_code == 409:
        body2 = r2.json().get("detail") or {}
        code2 = body2.get("code") or (
            (body2.get("context") or {}).get("blocked"))
        assert code2 != "identity", (
            f"identity gate still blocks after seeding: {body2}")
