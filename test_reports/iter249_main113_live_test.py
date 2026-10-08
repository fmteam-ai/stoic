"""iter249 — STOIC main113 Review live tests.

Covers:
- N113-1: paper account creation (AccountCreate validator paper branch).
- A17-13: Add Account wizard DEMO/REAL + algo_trading_consent + real_refused_demo_policy.
- GET /api/accounts/broker-presets → presets + demo_only_policy bool.
- POST /api/infra/agent/commands/ack with bogus agent_token → 401 (never 500).
- GET /api/setup/agent.ps1 → UTF-8 BOM, ASCII body, X-STOIC-SHA256 matches sha256(body).
"""
from __future__ import annotations
import hashlib
import os
import pathlib
import sys
import pytest
import requests

ROOT = pathlib.Path("/app")
sys.path.insert(0, str(ROOT / "backend"))


def _env(path, key):
    for line in (ROOT / path).read_text().splitlines():
        if line.startswith(key + "="):
            v = line.split("=", 1)[1].strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                v = v[1:-1]
            return v
    return None


BE = _env("frontend/.env", "REACT_APP_BACKEND_URL").rstrip("/")
ADMIN_EMAIL = _env("backend/.env", "TEST_ADMIN_EMAIL") or "admin@trading.bot"
MONGO_URL = _env("backend/.env", "MONGO_URL")
DB_NAME = _env("backend/.env", "DB_NAME")


def _password():
    pw = _env("backend/.env", "TEST_ADMIN_PASSWORD")
    if pw:
        return pw
    for line in (ROOT / "memory/test_credentials.md").read_text().splitlines():
        if line.strip().startswith("- Password:"):
            return line.split(":", 1)[1].strip()
    pytest.skip("no admin password available")


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    s.headers.update({"User-Agent": "stoic-iter249/1.0", "Content-Type": "application/json"})
    r = s.post(f"{BE}/api/auth/login", json={"email": ADMIN_EMAIL, "password": _password()}, timeout=20)
    if r.status_code != 200:
        pytest.skip(f"admin login failed: {r.status_code} {r.text[:200]}")
    tok = r.json().get("access_token") or r.json().get("token")
    if tok:
        s.headers["Authorization"] = f"Bearer {tok}"
    csrf = s.cookies.get("csrf_token")
    if csrf:
        s.headers["X-CSRF-Token"] = csrf
    me = s.get(f"{BE}/api/auth/me", timeout=20)
    assert me.status_code == 200, me.text[:200]
    s.user = me.json()
    return s


def _delete_account(s, acct_id):
    try:
        s.delete(f"{BE}/api/accounts/{acct_id}", timeout=20)
    except Exception:
        pass


# ── N113-1 paper account creation ─────────────────────────────────────────
def test_paper_account_sandbox_qa_ok(admin_session):
    body = {"label": "TEST_iter249_paper1", "broker": "", "server": "",
            "account_number": "SANDBOX-QA", "mode": "paper"}
    r = admin_session.post(f"{BE}/api/accounts", json=body, timeout=20)
    assert r.status_code == 200, r.text[:400]
    j = r.json()
    acct = j.get("account") or j
    acct_id = acct.get("id") or acct.get("_id")
    assert acct_id, j
    _delete_account(admin_session, acct_id)


def test_paper_account_numeric_sandbox_id_ok(admin_session):
    body = {"label": "TEST_iter249_paper2", "broker": "", "server": "",
            "account_number": "1001", "mode": "paper"}
    r = admin_session.post(f"{BE}/api/accounts", json=body, timeout=20)
    assert r.status_code == 200, r.text[:400]
    j = r.json()
    acct = j.get("account") or j
    acct_id = acct.get("id") or acct.get("_id")
    assert acct_id, j
    _delete_account(admin_session, acct_id)


def test_paper_account_bracket_charset_rejected(admin_session):
    body = {"label": "TEST_iter249_paper3", "broker": "", "server": "",
            "account_number": "SAND[1]", "mode": "paper"}
    r = admin_session.post(f"{BE}/api/accounts", json=body, timeout=20)
    assert r.status_code == 422, r.text[:400]


def test_paper_account_empty_number_rejected(admin_session):
    body = {"label": "TEST_iter249_paper4", "broker": "", "server": "",
            "account_number": "", "mode": "paper"}
    r = admin_session.post(f"{BE}/api/accounts", json=body, timeout=20)
    assert r.status_code == 422, r.text[:400]


def test_live_empty_server_rejected(admin_session):
    body = {"label": "TEST_iter249_live1", "broker": "X", "server": "",
            "account_number": "52012345", "mode": "live"}
    r = admin_session.post(f"{BE}/api/accounts", json=body, timeout=20)
    assert r.status_code == 422, r.text[:400]


def test_live_non_digit_login_rejected(admin_session):
    body = {"label": "TEST_iter249_live2", "broker": "IC Markets",
            "server": "ICMarketsSC-Demo", "account_number": "SANDBOX-1", "mode": "live"}
    r = admin_session.post(f"{BE}/api/accounts", json=body, timeout=20)
    assert r.status_code == 422, r.text[:400]


# ── A17-13: broker-presets + demo_only_policy + real_refused_demo_policy ──
def test_broker_presets_shape(admin_session):
    r = admin_session.get(f"{BE}/api/accounts/broker-presets", timeout=20)
    assert r.status_code == 200, r.text[:400]
    j = r.json()
    assert "presets" in j and isinstance(j["presets"], list)
    assert "demo_only_policy" in j and isinstance(j["demo_only_policy"], bool)


def _mongo_sync():
    try:
        from pymongo import MongoClient
    except ImportError:
        pytest.skip("pymongo not available")
    return MongoClient(MONGO_URL)[DB_NAME]


def test_live_real_declared_accepted_when_no_demo_policy(admin_session):
    db = _mongo_sync()
    exp = db.platform_state.find_one({"_id": "inventory_expectation"}) or {}
    if exp.get("demo_only"):
        pytest.skip("demo_only policy is active — real cannot be declared here")
    body = {"label": "TEST_iter249_real_decl", "broker": "IC Markets",
            "server": "ICMarketsSC-Demo", "account_number": "52019876",
            "mode": "live", "declared_environment": "real",
            "algo_trading_consent": True}
    r = admin_session.post(f"{BE}/api/accounts", json=body, timeout=20)
    assert r.status_code == 200, r.text[:500]
    j = r.json()
    acct = j.get("account") or j
    acct_id = acct.get("id") or acct.get("_id")
    assert acct.get("declared_environment") == "real", acct
    assert acct.get("algo_trading_consent_at"), acct
    _delete_account(admin_session, acct_id)


def test_real_refused_when_demo_only_policy_active(admin_session):
    """Flip demo_only=true on existing inventory_expectation (NOT upsert) and restore."""
    db = _mongo_sync()
    exp = db.platform_state.find_one({"_id": "inventory_expectation"})
    if not exp:
        pytest.skip("inventory_expectation doc does not exist — skipping (per instructions)")
    original_val = exp.get("demo_only")
    try:
        db.platform_state.update_one({"_id": "inventory_expectation"},
                                     {"$set": {"demo_only": True}})
        body = {"label": "TEST_iter249_real_refused", "broker": "IC Markets",
                "server": "ICMarketsSC-Demo", "account_number": "52019877",
                "mode": "live", "declared_environment": "real",
                "algo_trading_consent": True}
        r = admin_session.post(f"{BE}/api/accounts", json=body, timeout=20)
        assert r.status_code == 403, r.text[:500]
        detail = (r.json() or {}).get("detail") or {}
        code = detail.get("code") if isinstance(detail, dict) else None
        assert code == "real_refused_demo_policy", detail

        # demo declaration still accepted under the demo-only policy
        body2 = dict(body, declared_environment="demo",
                     account_number="52019878", label="TEST_iter249_demo_ok")
        r2 = admin_session.post(f"{BE}/api/accounts", json=body2, timeout=20)
        assert r2.status_code == 200, r2.text[:500]
        j2 = r2.json(); acct2 = j2.get("account") or j2
        _delete_account(admin_session, acct2.get("id") or acct2.get("_id"))
    finally:
        if original_val is None:
            db.platform_state.update_one({"_id": "inventory_expectation"},
                                         {"$unset": {"demo_only": ""}})
        else:
            db.platform_state.update_one({"_id": "inventory_expectation"},
                                         {"$set": {"demo_only": original_val}})


# ── /api/infra/agent/commands/ack bogus agent_token → 401 (not 500) ──────
def test_ack_bogus_token_401():
    r = requests.post(f"{BE}/api/infra/agent/commands/ack",
                      json={"agent_token": "agt_tok_definitely_not_real_xxxxxxxx",
                            "command_id": "cmd_doesnotexist",
                            "ok": False, "detail": "seq_replay: agent last_seq=5",
                            "last_seq": 5}, timeout=20)
    assert r.status_code == 401, f"got {r.status_code}: {r.text[:300]}"


# ── GET /api/setup/agent.ps1 BOM + ASCII + X-STOIC-SHA256 header ──────────
def test_agent_ps1_bom_and_sha_header():
    r = requests.get(f"{BE}/api/setup/agent.ps1", timeout=20)
    assert r.status_code == 200, r.text[:200]
    body = r.content
    assert body[:3] == b"\xef\xbb\xbf", f"first 3 bytes: {body[:3].hex()}"
    non_ascii = [i for i, x in enumerate(body[3:]) if x >= 0x80]
    assert not non_ascii, f"non-ASCII bytes after BOM at offsets {non_ascii[:5]}"
    header_sha = r.headers.get("X-STOIC-SHA256")
    assert header_sha, "X-STOIC-SHA256 header missing"
    assert header_sha == hashlib.sha256(body).hexdigest(), \
        f"header={header_sha} body_sha={hashlib.sha256(body).hexdigest()}"
