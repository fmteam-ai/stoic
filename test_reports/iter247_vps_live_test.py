"""iter247 — live tests for Phase 2 VPS Agent features: (1) agent enrol one-liner on
Path B connect-existing, and (2) POST /vps/agents/{agent_id}/restart-terminal.

Hits the preview backend (REACT_APP_BACKEND_URL) using the admin HTTP session, and
seeds / tears down a vps_agents + mt5_instances fixture directly on MongoDB
(MONGO_URL/DB_NAME from backend/.env). Nothing is left behind.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import pathlib
import re
import secrets
import sys

import pytest
import requests
from bson import ObjectId

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
    s.headers.update({"User-Agent": "stoic-iter247/1.0", "Content-Type": "application/json"})
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


# ── Feature 1: Agent enrol one-liner on Path B connect-existing ────────────
def test_connect_existing_returns_hash_pinned_agent_command(admin_session):
    body = {"hostname": "test-vps-iter247", "label": "iter247", "os": "windows-server",
            "mt5_installed": True, "region": "us-east", "mt5_instances": 1}
    r = admin_session.post(f"{BE}/api/infra/vps/connect-existing", json=body, timeout=30)
    assert r.status_code == 200, r.text[:400]
    data = r.json()
    assert "deployment_id" in data
    code = data.get("enrollment_code")
    sha = data.get("vps_agent_sha256") or ""
    cmd = data.get("vps_agent_command") or ""
    assert code and re.match(r"^[A-Z]{4}-\d{4}$", code), f"bad enrollment code: {code!r}"
    assert re.fullmatch(r"[0-9a-f]{64}", sha), f"sha256 should be 64 hex: {sha!r}"
    assert "/api/setup/agent.ps1" in cmd
    assert "Install-StoicAgent -ServerUrl" in cmd
    assert f'-EnrollmentCode "{code}"' in cmd
    assert sha.upper() in cmd, "command must contain the UPPERCASED sha256 pin"
    # The served script body SHA-256 must match vps_agent_sha256.
    ps1 = requests.get(f"{BE}/api/setup/agent.ps1", timeout=20)
    assert ps1.status_code == 200
    body_sha = hashlib.sha256(ps1.content).hexdigest()
    assert body_sha == sha, f"body sha {body_sha} != advertised {sha}"


# ── Feature 2: restart-terminal ────────────────────────────────────────────
def test_restart_terminal_unauth():
    r = requests.post(f"{BE}/api/vps/agents/agt_whatever/restart-terminal",
                      json={"account_id": "000000000000000000000000"}, timeout=20)
    assert r.status_code in (401, 403), f"expected 401/403 got {r.status_code}: {r.text[:200]}"


def test_restart_terminal_unknown_agent(admin_session):
    r = admin_session.post(f"{BE}/api/vps/agents/agt_doesnotexist/restart-terminal",
                           json={"account_id": "000000000000000000000000"}, timeout=20)
    assert r.status_code == 404, r.text[:300]
    j = r.json()
    detail = j.get("detail") or {}
    code = detail.get("code") if isinstance(detail, dict) else None
    assert code == "vps_restart_refused", f"detail={detail!r}"


def _mongo_sync():
    """Use a sync Mongo client for simplicity in test setup/teardown."""
    try:
        from pymongo import MongoClient
    except ImportError:
        pytest.skip("pymongo not available")
    return MongoClient(MONGO_URL)[DB_NAME]


def _hash_agent_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@pytest.fixture
def seeded_agent(admin_session):
    """Insert a fake online vps_agents doc for the admin and tear it down."""
    db = _mongo_sync()
    user_id = admin_session.user["id"]
    # Need an admin account (mode != paper) with an account_number for the login.
    acct = db.accounts.find_one({"user_id": user_id, "mode": {"$ne": "paper"},
                                 "account_number": {"$exists": True, "$ne": None}})
    if not acct:
        pytest.skip("admin has no live MT5 account to pin a terminal to")
    agent_id = f"agt_iter247_{secrets.token_hex(4)}"
    agent_token = f"agt_tok_{secrets.token_urlsafe(32)}"
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    db.vps_agents.insert_one({
        "agent_id": agent_id, "user_id": user_id,
        "agent_token_hash": _hash_agent_token(agent_token),
        "revoked": False, "command_seq": 0,
        "last_heartbeat": now, "registered_at": now,
        "deployment_id": "dep_test_iter247",
    })
    yield {"agent_id": agent_id, "agent_token": agent_token,
           "account_id": str(acct["_id"]), "account_number": str(acct["account_number"])}
    # Teardown
    db.vps_agents.delete_many({"agent_id": agent_id})
    db.mt5_instances.delete_many({"agent_id": agent_id})
    db.agent_commands.delete_many({"agent_id": agent_id})
    db.accounts.update_one({"_id": acct["_id"]}, {"$unset": {"vps_terminal": ""}})


def test_restart_terminal_without_instance_409(admin_session, seeded_agent):
    r = admin_session.post(
        f"{BE}/api/vps/agents/{seeded_agent['agent_id']}/restart-terminal",
        json={"account_id": seeded_agent["account_id"]}, timeout=20)
    assert r.status_code == 409, r.text[:400]
    detail = r.json().get("detail") or {}
    code = detail.get("code") if isinstance(detail, dict) else None
    msg = (detail.get("message") if isinstance(detail, dict) else "") or r.text
    assert code == "vps_restart_refused"
    # Note: static_error maps the ValueError("no agent-managed terminal ... install it on the VPS first")
    # into a canned client-safe message; the original phrasing is only in server logs.
    _ = msg


def test_restart_terminal_success_after_report(admin_session, seeded_agent):
    # Step 1 — create the mt5_instances doc via /vps/agent/terminals/report
    report_payload = {
        "agent_token": seeded_agent["agent_token"],
        "login": seeded_agent["account_number"],
        "account_id": seeded_agent["account_id"],
        "status": "running",
    }
    r = requests.post(f"{BE}/api/vps/agent/terminals/report", json=report_payload, timeout=20)
    assert r.status_code == 200, r.text[:300]

    # Step 2 — admin queues a restart
    r2 = admin_session.post(
        f"{BE}/api/vps/agents/{seeded_agent['agent_id']}/restart-terminal",
        json={"account_id": seeded_agent["account_id"]}, timeout=20)
    assert r2.status_code == 200, r2.text[:400]
    j = r2.json()
    assert j.get("ok") is True
    cmd_id = j.get("command_id")
    assert cmd_id and cmd_id.startswith("cmd_")
    assert j.get("agent_id") == seeded_agent["agent_id"]
    assert j.get("login") == seeded_agent["account_number"]

    # Step 3 — poll_commands as the agent must return the restart_terminal command
    r3 = requests.post(f"{BE}/api/infra/agent/commands/poll",
                       json={"agent_token": seeded_agent["agent_token"]}, timeout=20)
    assert r3.status_code == 200, r3.text[:300]
    cmds = r3.json().get("commands") or r3.json()
    if isinstance(cmds, dict):
        cmds = cmds.get("commands") or []
    restart = [c for c in cmds if c.get("command") == "restart_terminal"]
    assert restart, f"no restart_terminal in poll result: {cmds}"
    assert restart[0]["params"].get("login") == seeded_agent["account_number"]
