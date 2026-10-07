"""Easy MT5 Connect Phase 1 — live preview HTTP checks.

Covers:
  · GET /api/ea-script (default + ?v=1.61) → EA 1.61 source with autoload + self-check
  · GET /api/setup/installer.ps1 → v1.5 installer with running-terminal picker + TLS 1.2
  · POST /api/setup/pairing-token → ttl_minutes == 60 and single-outstanding invariant
  · POST /api/setup/claim-pairing → returns bridge_token
  · POST /api/bridge/heartbeat with ea_self_check (valid + invalid token)
  · GET /api/setup/install-progress/{account_id} → heartbeat step status reflects flags
"""
from __future__ import annotations
import os, re, uuid, time
from datetime import datetime, timezone, timedelta
import pytest, requests

pytestmark = pytest.mark.http   # live-server lane (same as test_iter84_pairing_flow)
from bson import ObjectId
from pymongo import MongoClient

def _base_url():
    v = os.environ.get("REACT_APP_BACKEND_URL")
    if v:
        return v.rstrip("/")
    with open("/app/frontend/.env") as f:
        for ln in f:
            if ln.startswith("REACT_APP_BACKEND_URL="):
                return ln.split("=", 1)[1].strip().rstrip("/")
    raise RuntimeError("REACT_APP_BACKEND_URL not set")


BASE_URL = _base_url()
TIMEOUT = 30


def _mongo():
    env = {}
    with open("/app/backend/.env") as f:
        for ln in f:
            if "=" in ln:
                k, v = ln.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return MongoClient(env["MONGO_URL"])[env["DB_NAME"]]


def _csrf(sess):
    tok = sess.cookies.get("csrf_token")
    return {"x-csrf-token": tok} if tok else {}


def _register_and_login():
    suffix = uuid.uuid4().hex[:10]
    email, pw = f"qa_easyconn_{suffix}@example.com", "Gy6#Vb3kM9zRnD2s"
    r = requests.post(f"{BASE_URL}/api/auth/register",
                      json={"email": email, "password": pw,
                            "name": "QA-easyconnect", "terms_agreed": True},
                      timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    uid = r.json()["id"]
    _mongo().users.update_one(
        {"_id": ObjectId(uid)},
        {"$set": {"email_verified": True},
         "$unset": {"activation_token": "", "activation_expires_at": ""}},
    )
    s = requests.Session()
    r2 = s.post(f"{BASE_URL}/api/auth/login",
                json={"email": email, "password": pw}, timeout=TIMEOUT)
    assert r2.status_code == 200, r2.text
    return uid, s


def _add_live_account(sess):
    acct = "qa-" + uuid.uuid4().hex[:8]
    r = sess.post(f"{BASE_URL}/api/accounts", json={
        "label": "QA-easyconnect", "broker": "STARTRADER", "server": "T-Demo",
        "account_number": acct, "account_type": "demo",
        "base_currency": "USD", "mode": "live",
    }, headers=_csrf(sess), timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return r.json()["id"]


@pytest.fixture(scope="module")
def ctx():
    uid, sess = _register_and_login()
    aid = _add_live_account(sess)
    yield {"uid": uid, "sess": sess, "aid": aid}
    db = _mongo()
    db.users.delete_many({"_id": ObjectId(uid)})
    db.accounts.delete_many({"_id": ObjectId(aid)})
    db.pairing_tokens.delete_many({"account_id": aid})


# ─────────────── EA script ───────────────
def test_ea_script_1_61_autoload_and_self_check():
    for url in (f"{BASE_URL}/api/ea-script", f"{BASE_URL}/api/ea-script?v=1.61"):
        r = requests.get(url, timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        body = r.text
        assert '#define EA_CLIENT_VERSION "1.61"' in body
        assert "string ResolveServerUrl" in body
        assert "ea_self_check" in body
        assert 'ServerUrl + "' not in body
        assert 'input string ServerUrl              = "https://www.stoicaibot.com"' in body


# ─────────────── Installer ───────────────
def test_installer_ps1_v1_5_zero_touch():
    r = requests.get(f"{BASE_URL}/api/setup/installer.ps1", timeout=TIMEOUT)
    assert r.status_code == 200
    body = r.text
    for needle in ('$InstallerVersion = "1.5"',
                   "function Get-StoicRunningTerminal",
                   "STOIC-Server.txt",
                   "stoic.set",
                   "stoic-start.ini",
                   "function Restart-StoicTerminal",
                   "SecurityProtocolType]::Tls12"):
        assert needle in body, f"missing in installer: {needle!r}"


# ─────────────── Pairing token TTL=60 + single-outstanding ───────────────
def test_pairing_token_ttl_60_and_single_outstanding(ctx):
    sess, aid = ctx["sess"], ctx["aid"]
    r = sess.post(f"{BASE_URL}/api/setup/pairing-token",
                  json={"account_id": aid}, headers=_csrf(sess), timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ttl_minutes"] == 60
    exp = datetime.fromisoformat(body["expires_at"])
    delta = exp - datetime.now(timezone.utc)
    assert 58 * 60 < delta.total_seconds() < 62 * 60, f"delta={delta}"
    t1 = body["token"]

    # Issue again → invalidates previous
    r2 = sess.post(f"{BASE_URL}/api/setup/pairing-token",
                   json={"account_id": aid}, headers=_csrf(sess), timeout=TIMEOUT)
    assert r2.status_code == 200
    t2 = r2.json()["token"]
    assert t2 != t1
    # Previous token must be gone from pairing_tokens
    assert _mongo().pairing_tokens.find_one({"token": t1}) is None
    assert _mongo().pairing_tokens.find_one({"token": t2}) is not None


# ─────────────── Heartbeat w/ ea_self_check ───────────────
def _issue_and_claim_bridge_token(sess, aid) -> str:
    tok = sess.post(f"{BASE_URL}/api/setup/pairing-token",
                    json={"account_id": aid}, headers=_csrf(sess), timeout=TIMEOUT).json()["token"]
    r = requests.post(f"{BASE_URL}/api/setup/claim-pairing",
                      json={"token": tok, "hostname": "qa-vps",
                            "installer_version": "1.5"},
                      timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return r.json()["bridge_token"]


def test_heartbeat_with_self_check_warn_then_done_and_strips_junk(ctx):
    sess, aid = ctx["sess"], ctx["aid"]
    bt = _issue_and_claim_bridge_token(sess, aid)

    # autotrading false → warn + mentions AutoTrading
    r = requests.post(f"{BASE_URL}/api/bridge/heartbeat",
                      json={"bridge_token": bt,
                            "balance": 1000.0, "equity": 1000.0, "margin": 0.0, "free_margin": 1000.0, "open_positions": 0, "ea_self_check": {"autotrading": False,
                                              "ea_trade_allowed": True,
                                              "webrequest_ok": True,
                                              "junk": "x"}},
                      timeout=TIMEOUT)
    assert r.status_code == 200, r.text

    # Non-bool "junk" dropped
    acct = _mongo().accounts.find_one({"_id": ObjectId(aid)})
    sc = acct.get("ea_self_check") or {}
    assert "junk" not in sc
    assert sc.get("autotrading") is False
    assert sc.get("ea_trade_allowed") is True
    assert sc.get("webrequest_ok") is True

    r = sess.get(f"{BASE_URL}/api/setup/install-progress/{aid}", timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    steps = {s["id"]: s for s in r.json().get("steps", [])}
    hb = steps.get("heartbeat")
    assert hb, r.json()
    assert hb["status"] == "warn", hb
    assert "AutoTrading" in (hb.get("hint") or "")

    # All flags true → done
    r = requests.post(f"{BASE_URL}/api/bridge/heartbeat",
                      json={"bridge_token": bt,
                            "balance": 1000.0, "equity": 1000.0, "margin": 0.0, "free_margin": 1000.0, "open_positions": 0, "ea_self_check": {"autotrading": True,
                                              "ea_trade_allowed": True,
                                              "webrequest_ok": True}},
                      timeout=TIMEOUT)
    assert r.status_code == 200
    r = sess.get(f"{BASE_URL}/api/setup/install-progress/{aid}", timeout=TIMEOUT)
    hb = next(s for s in r.json()["steps"] if s["id"] == "heartbeat")
    assert hb["status"] == "done", hb


def test_heartbeat_invalid_bridge_token_401():
    r = requests.post(f"{BASE_URL}/api/bridge/heartbeat",
                      json={"bridge_token": "not-a-real-token-xxxxxxxxxxxxx",
                            "balance": 1.0, "equity": 1.0, "margin": 0.0,
                            "free_margin": 1.0, "open_positions": 0},
                      timeout=TIMEOUT)
    assert r.status_code == 401, r.text
    assert "Invalid bridge token" in r.text
