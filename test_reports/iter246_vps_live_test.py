"""iter246 — live smoke of VPS Agent Service routes against the preview backend.

Confirms the admin can:
  1. Download the agent bootstrap (/api/setup/agent.ps1)
  2. GET /api/vps/agents (entitlement vps_quick_connect short-circuits for admin)
  3. POST /api/vps/agents/<agent_id>/install-terminal with no agents → 404 vps_install_refused
Also probes that unauthenticated callers get a 401 on the dashboard endpoint.
"""
from __future__ import annotations

import os
import pathlib

import pytest
import requests

ROOT = pathlib.Path("/app")


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


def _password():
    # Never embed. Read from .env, then from the published credentials memo.
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
    s.headers.update({"User-Agent": "stoic-iter246-live/1.0", "Content-Type": "application/json"})
    r = s.post(f"{BE}/api/auth/login", json={"email": ADMIN_EMAIL, "password": _password()}, timeout=20)
    if r.status_code != 200:
        pytest.skip(f"admin login failed: {r.status_code} {r.text[:200]}")
    tok = r.json().get("access_token") or r.json().get("token")
    if tok:
        s.headers["Authorization"] = f"Bearer {tok}"
    csrf = s.cookies.get("csrf_token")
    if csrf:
        s.headers["X-CSRF-Token"] = csrf
    return s


def test_agent_bootstrap_ps1_served():
    r = requests.get(f"{BE}/api/setup/agent.ps1", timeout=20)
    assert r.status_code == 200, r.text[:200]
    body = r.text
    assert "STOIC VPS Agent" in body or "STOIC-Agent" in body or "Install-Stoic" in body or "agent_token" in body
    assert "/api/infra/agent/register" in body
    assert "/api/vps/agent/terminals/status" in body
    assert "/api/vps/agent/terminals/report" in body


def test_my_agents_requires_auth():
    r = requests.get(f"{BE}/api/vps/agents", timeout=20)
    assert r.status_code in (401, 403), f"expected 401/403 got {r.status_code}"


def test_my_agents_admin_ok(admin_session):
    r = admin_session.get(f"{BE}/api/vps/agents", timeout=20)
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert "agents" in body and isinstance(body["agents"], list)


def test_install_terminal_admin_no_agent_returns_404(admin_session):
    # bogus agent_id → 404 vps_install_refused (NOT 402, which would mean entitlement blocked admin)
    r = admin_session.post(
        f"{BE}/api/vps/agents/agt_does_not_exist/install-terminal",
        json={"account_id": "66f000000000000000000001", "chart_symbol": "EURUSD"},
        timeout=20,
    )
    assert r.status_code != 402, "admin should bypass vps_quick_connect entitlement"
    assert r.status_code in (404, 409), f"unexpected: {r.status_code} {r.text[:300]}"
    body = r.json()
    assert body.get("code") == "vps_install_refused" or "vps_install_refused" in r.text
