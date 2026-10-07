"""Live-preview HTTP tests for STOIC main110 corrections (iter 244).

Covers:
 - /api/ops/alerts response shape (alerts, mutes, snoozed_until)
 - /api/ops/alerts/mute POST (good + bad) + DELETE
 - raise_alert against the real db shows notify_muted_until after a human ack
 - /api/ea-script.ex5 -> 409 ex5_not_published
 - /api/setup/installer.ps1 -> 200 + required substrings
 - POST /api/setup/pairing-token install_command starts with TLS 1.2 prefix

Credentials are read from backend/.env (TEST_ADMIN_EMAIL/PASSWORD) with a
fallback to admin@trading.bot per /app/memory/test_credentials.md — values are
never hard-coded here.
"""

from __future__ import annotations

import os
import asyncio
import sys
import pathlib
from datetime import datetime, timezone

import pytest
import requests
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient

sys.path.insert(0, "/app/backend")

# --- env ---------------------------------------------------------------
def _read_env(path: str) -> dict:
    out = {}
    try:
        for ln in pathlib.Path(path).read_text().splitlines():
            if "=" in ln and not ln.strip().startswith("#"):
                k, _, v = ln.partition("=")
                out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


BE = _read_env("/app/backend/.env")
FE = _read_env("/app/frontend/.env")

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL") or FE["REACT_APP_BACKEND_URL"]
BASE_URL = BASE_URL.rstrip("/")
MONGO_URL = BE["MONGO_URL"]
DB_NAME = "ai_trading_bot"
ADMIN_EMAIL = os.environ.get("TEST_ADMIN_EMAIL") or BE.get("TEST_ADMIN_EMAIL") or "admin@trading.bot"
ADMIN_PASSWORD = os.environ.get("TEST_ADMIN_PASSWORD") or BE.get("TEST_ADMIN_PASSWORD")
if not ADMIN_PASSWORD:
    # fallback: /app/memory/test_credentials.md contains the password (not embedded here)
    import re
    try:
        md = pathlib.Path("/app/memory/test_credentials.md").read_text()
        m = re.search(r"Password:\s*(\S+)", md)
        if m:
            ADMIN_PASSWORD = m.group(1).strip('"').strip("'")
    except FileNotFoundError:
        pass

ALT_ADMIN = "admin@stoicaibot.com"
TEST_DEDUP = "ea_heartbeat:test-main110"


# --- helpers -----------------------------------------------------------
def _login(email: str) -> requests.Session | None:
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login", json={"email": email, "password": ADMIN_PASSWORD}, timeout=20)
    if r.status_code != 200:
        return None
    j = r.json()
    if j.get("mfa_required") or j.get("totp_required"):
        return None
    csrf = s.cookies.get("csrf_token")
    if csrf:
        s.headers.update({"x-csrf-token": csrf})
    return s


@pytest.fixture(scope="module")
def admin_session():
    for em in (ADMIN_EMAIL, ALT_ADMIN):
        s = _login(em)
        if s is not None:
            return s
    pytest.skip("Admin login requires TOTP in preview — reporting and skipping")


@pytest.fixture(scope="module")
def db():
    client = AsyncIOMotorClient(MONGO_URL)
    return client[DB_NAME]


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if asyncio.get_event_loop_policy() else asyncio.run(coro)


# --- tests -------------------------------------------------------------
def test_ea_ex5_409(admin_session):
    r = requests.get(f"{BASE_URL}/api/ea-script.ex5", timeout=15)
    assert r.status_code == 409
    assert r.json().get("error") == "ex5_not_published"


def test_installer_ps1():
    r = requests.get(f"{BASE_URL}/api/setup/installer.ps1", timeout=15)
    assert r.status_code == 200
    body = r.text
    assert '$InstallerVersion = "1.6"' in body
    assert "Restart MetaTrader 5 now? [y/N]" in body
    assert "X-STOIC-EA-Version" in body
    assert "AllowDllImport" not in body
    assert "Stop-Process" not in body


def test_ops_alerts_shape(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/ops/alerts", timeout=15)
    assert r.status_code == 200, r.text
    j = r.json()
    assert "alerts" in j and isinstance(j["alerts"], list)
    assert "mutes" in j and isinstance(j["mutes"], list)
    for a in j["alerts"]:
        assert "snoozed_until" in a
        assert a["snoozed_until"] is None or isinstance(a["snoozed_until"], str)


def test_mute_lifecycle(admin_session):
    # bad kind
    r = admin_session.post(f"{BASE_URL}/api/ops/alerts/mute", json={"kind": "bogus", "hours": 24})
    assert r.status_code == 400
    # too long
    r = admin_session.post(f"{BASE_URL}/api/ops/alerts/mute", json={"kind": "pairing_no_heartbeat", "hours": 48})
    assert r.status_code == 400
    # good
    r = admin_session.post(f"{BASE_URL}/api/ops/alerts/mute", json={"kind": "pairing_no_heartbeat", "hours": 24})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["kind"] == "pairing_no_heartbeat"
    muted_until = datetime.fromisoformat(body["muted_until"])
    delta_h = (muted_until - datetime.now(timezone.utc)).total_seconds() / 3600
    assert 23.5 < delta_h <= 24.1, delta_h

    # listed under mutes
    r = admin_session.get(f"{BASE_URL}/api/ops/alerts", timeout=15)
    assert r.status_code == 200
    mutes = {m["kind"] for m in r.json()["mutes"]}
    assert "pairing_no_heartbeat" in mutes

    # unmute
    r = admin_session.delete(f"{BASE_URL}/api/ops/alerts/mute/pairing_no_heartbeat")
    assert r.status_code == 200
    assert r.json()["removed"] == 1


async def _ack_and_reraise(db):
    import alerting as _alerting
    now = datetime.now(timezone.utc)
    await db.ops_alerts.delete_many({"dedup_key": TEST_DEDUP})
    ins = await db.ops_alerts.insert_one({
        "kind": "ea_heartbeat_stale", "severity": "critical", "message": "test",
        "dedup_key": TEST_DEDUP, "acked_at": None, "created_at": now,
        "synthetic": False, "occurrences": 1, "meta": {}, "last_seen_at": now,
    })
    return str(ins.inserted_id)


def test_raise_alert_after_ack_sets_notify_muted(admin_session, db):
    inserted_id = asyncio.get_event_loop().run_until_complete(_ack_and_reraise(db))
    # ack via API (as human admin)
    r = admin_session.post(f"{BASE_URL}/api/ops/alerts/{inserted_id}/ack")
    assert r.status_code == 200, r.text

    async def _work():
        import alerting as _alerting
        # row remains open? no — ack marks acked_at. Now re-raise: must insert a NEW unacked row with notify_muted_until ~6h out
        new_id = await _alerting.raise_alert(
            db, "ea_heartbeat_stale", "critical", "test", dedup_key=TEST_DEDUP, meta={})
        assert new_id is not None, "re-raise should insert a new row since previous was acked"
        row = await db.ops_alerts.find_one({"_id": ObjectId(new_id)})
        assert row["acked_at"] is None
        nm = row.get("notify_muted_until")
        assert nm is not None, "notify_muted_until should be set after human ack within ACK_SUPPRESS window"
        if nm.tzinfo is None:
            nm = nm.replace(tzinfo=timezone.utc)
        delta_h = (nm - datetime.now(timezone.utc)).total_seconds() / 3600
        assert 5.5 < delta_h <= 6.1, f"notify_muted_until should be ~6h ahead, got {delta_h}"

    asyncio.get_event_loop().run_until_complete(_work())
    # cleanup
    asyncio.get_event_loop().run_until_complete(db.ops_alerts.delete_many({"dedup_key": TEST_DEDUP}))


def test_pairing_token_install_command_tls12(admin_session, db):
    # find any existing account for admin; else create a live one
    async def _find_or_create():
        me = admin_session.get(f"{BASE_URL}/api/auth/me", timeout=10).json()
        user_id = me.get("id") or me.get("user", {}).get("id")
        acc = await db.accounts.find_one({"user_id": user_id, "mode": "live"})
        if acc:
            return str(acc["_id"]) if "_id" in acc else acc.get("id"), False
        return None, False

    acc_id, created = asyncio.get_event_loop().run_until_complete(_find_or_create())
    if not acc_id:
        r = admin_session.post(f"{BASE_URL}/api/accounts", json={
            "mode": "live", "broker": "STARTRADER", "server": "STARTRADER-Live",
            "login": "99999999", "account_name": "QA-main110",
        })
        assert r.status_code in (200, 201), r.text
        acc_id = r.json().get("id") or r.json().get("account_id")
        created = True
    assert acc_id

    r = admin_session.post(f"{BASE_URL}/api/setup/pairing-token", json={"account_id": acc_id})
    assert r.status_code == 200, r.text
    j = r.json()
    install_cmd = j.get("install_command") or ""
    assert install_cmd.startswith(
        "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $r=iwr"
    ), f"install_command missing TLS1.2 prefix: {install_cmd[:120]}"

    if created:
        asyncio.get_event_loop().run_until_complete(db.accounts.delete_one({"_id": ObjectId(acc_id)}))
