"""Live-preview HTTP tests for STOIC main111 corrections (iter 245).

Covers (from /app/backend/tests/unit/test_fixplan_main111.py):
 N111-1  /api/ops/alerts: survives naive BSON datetimes and includes `mutable_kinds`.
         `snoozed_until` is ISO with timezone (ends with +00:00) when set.
 N111-6  /api/ops/alerts.mutable_kinds matches alerting.EVALUATOR_KINDS, AND
         ea_latest_version is derived (currently "1.62" from the MQ5 source).
 N111-2  POST /api/setup/claim-pairing-token with wrong `terminal_login` → 409
         `account_mismatch` AND the pairing_tokens row is NOT consumed.

Credentials read from backend/.env / /app/memory/test_credentials.md (never embedded).
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import re
import sys
import secrets
from datetime import datetime, timedelta, timezone

import pytest
import requests
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient

sys.path.insert(0, "/app/backend")


def _read_env(path):
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
BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL") or FE["REACT_APP_BACKEND_URL"]).rstrip("/")
MONGO_URL = BE["MONGO_URL"]
DB_NAME = "ai_trading_bot"
ADMIN_EMAIL = os.environ.get("TEST_ADMIN_EMAIL") or BE.get("TEST_ADMIN_EMAIL") or "admin@trading.bot"
ADMIN_PASSWORD = os.environ.get("TEST_ADMIN_PASSWORD") or BE.get("TEST_ADMIN_PASSWORD")
if not ADMIN_PASSWORD:
    try:
        md = pathlib.Path("/app/memory/test_credentials.md").read_text()
        m = re.search(r"Password:\s*(\S+)", md)
        if m:
            ADMIN_PASSWORD = m.group(1).strip('"').strip("'")
    except FileNotFoundError:
        pass
ALT_ADMIN = "admin@stoicaibot.com"


def _login(email):
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
    pytest.skip("Admin login requires TOTP in preview")


@pytest.fixture(scope="module")
def db():
    client = AsyncIOMotorClient(MONGO_URL)
    return client[DB_NAME]


def _run(coro):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


# ── N111-1 / N111-6 ──────────────────────────────────────────────────────────
def test_ops_alerts_includes_mutable_kinds_and_iso_tz(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/ops/alerts", timeout=20)
    assert r.status_code == 200, r.text
    j = r.json()
    assert "mutable_kinds" in j, "N111-6: /api/ops/alerts must include mutable_kinds"
    mk = j["mutable_kinds"]
    assert isinstance(mk, list) and len(mk) >= 10
    # EVALUATOR_KINDS from alerting.py — must be exactly these 12
    expected = {
        "ea_heartbeat_stale", "worker_lease_expired", "worker_loop_crashloop",
        "worker_loop_stalled", "outbox_backlog", "outbox_failed",
        "unprotected_positions", "reconciliation_stuck", "pairing_no_heartbeat",
        "policy_expiring", "policy_expired", "demo_account_reports_real",
    }
    assert set(mk) == expected, f"mutable_kinds drift: {set(mk) ^ expected}"
    # alerts rows: snoozed_until, if set, has +00:00 suffix (aware ISO)
    for a in j["alerts"]:
        s = a.get("snoozed_until")
        if s is not None:
            assert isinstance(s, str) and s.endswith("+00:00"), s
    for m in j["mutes"]:
        assert m["muted_until"].endswith("+00:00")


def test_ops_alerts_survives_naive_bson_datetimes(admin_session, db):
    """N111-1: insert an ops_alerts row with naive datetimes; list must still 200."""
    naive = datetime.utcnow()
    dedup = f"test-main111-naive-{secrets.token_hex(4)}"
    doc = {
        "kind": "ea_heartbeat_stale", "severity": "critical", "message": "n111 naive probe",
        "dedup_key": dedup, "acked_at": None, "created_at": naive, "last_seen_at": naive,
        "notify_muted_until": naive + timedelta(hours=5), "occurrences": 1, "meta": {},
    }
    inserted = _run(db.ops_alerts.insert_one(doc)).inserted_id
    try:
        r = admin_session.get(f"{BASE_URL}/api/ops/alerts", timeout=20)
        assert r.status_code == 200, r.text
        j = r.json()
        probe = next((a for a in j["alerts"] if a.get("dedup_key") == dedup), None)
        assert probe is not None, f"inserted dedup {dedup} not visible"
        assert probe["snoozed_until"] is not None
        assert probe["snoozed_until"].endswith("+00:00")
        # Must be ~5h out (we set it to naive+5h)
        delta_h = (datetime.fromisoformat(probe["snoozed_until"]) - datetime.now(timezone.utc)).total_seconds() / 3600
        assert 4.5 < delta_h <= 5.1, delta_h
    finally:
        _run(db.ops_alerts.delete_one({"_id": inserted}))


# ── N111-2 + N111-6 — claim-pairing wrong terminal_login → 409 BEFORE consume,
#                     then correct retry succeeds and exposes derived ea_latest_version
def test_claim_pairing_wrong_terminal_then_correct(admin_session, db):
    me = admin_session.get(f"{BASE_URL}/api/auth/me", timeout=10).json()
    uid = me.get("id") or me.get("user", {}).get("id")

    async def _find():
        return await db.accounts.find_one({"user_id": uid, "mode": "live"})
    acc = _run(_find())
    created_here = False
    if not acc:
        r = admin_session.post(f"{BASE_URL}/api/accounts", json={
            "mode": "live", "broker": "STARTRADER", "server": "STARTRADER-Live",
            "login": "77776666", "account_name": "QA-main111-claim",
        })
        assert r.status_code in (200, 201), r.text
        acc_id = r.json().get("id") or r.json().get("account_id")
        real_login = "77776666"
        created_here = True
    else:
        acc_id = str(acc["_id"])
        real_login = str(acc.get("account_number") or "").strip()
    if not real_login:
        pytest.skip("admin's live account has no account_number — cannot exercise terminal_login match")

    try:
        # 1) Issue a pairing token
        r = admin_session.post(f"{BASE_URL}/api/setup/pairing-token", json={"account_id": acc_id}, timeout=15)
        assert r.status_code == 200, r.text
        token = r.json()["token"]

        # Snapshot the pairing_tokens row to prove it wasn't consumed on mismatch
        async def _snap():
            return await db.pairing_tokens.find_one({"token": token})
        before = _run(_snap())
        assert before is not None, "pairing_tokens row missing after generate"
        assert "consumed_at" not in before or before.get("consumed_at") is None

        # 2) Wrong terminal_login → 409 account_mismatch, token NOT consumed
        wrong_login = str(int(real_login) + 1)
        r = requests.post(
            f"{BASE_URL}/api/setup/claim-pairing",
            json={"token": token, "terminal_login": wrong_login, "hostname": "qa-main111"},
            timeout=20,
        )
        assert r.status_code == 409, (r.status_code, r.text)
        payload = r.json().get("detail") or r.json()
        code = payload.get("code") if isinstance(payload, dict) else None
        assert code == "account_mismatch", r.text

        after = _run(_snap())
        assert after is not None
        assert after.get("consumed_at") in (None,), "N111-2: token MUST NOT be consumed on mismatch"

        # 3) Correct terminal_login → 200 with ea_latest_version derived (= "1.62")
        r = requests.post(
            f"{BASE_URL}/api/setup/claim-pairing",
            json={"token": token, "terminal_login": real_login, "hostname": "qa-main111"},
            timeout=20,
        )
        assert r.status_code == 200, r.text
        j = r.json()
        assert j.get("ea_latest_version") == "1.62", j.get("ea_latest_version")
        assert j.get("bridge_token"), "claim-pairing must return bridge_token on success"
        # token is consumed now
        after2 = _run(_snap())
        assert after2.get("consumed_at") is not None
    finally:
        if created_here:
            _run(db.accounts.delete_one({"_id": ObjectId(acc_id)}))
