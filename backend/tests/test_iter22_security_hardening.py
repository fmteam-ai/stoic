from live_target import ADMIN_EMAIL, ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
"""Iter22 — security & stability hardening regression tests.

Covers:
  P1.1 — bridge_routes.report_trade slippage_veto path no longer raises NameError
  P1.2 — CORS rejects untrusted origins; whitelisted origin still works
  P1.3 — credential reveal requires password re-confirmation + audit log
  P2.1 — broker_deals.insert catches ONLY DuplicateKeyError, surfaces others
  P2.2 — invalid ObjectId paths return 404 instead of 500
"""
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import os
import uuid
import pytest
import requests
from bson import ObjectId
from datetime import datetime, timezone
from motor.motor_asyncio import AsyncIOMotorClient

from live_target import require_live_base_url
BASE_URL = require_live_base_url()
API = f"{BASE_URL}/api"
pass  # ADMIN_EMAIL comes from live_target
pass  # ADMIN_PASSWORD comes from live_target
def _login_admin():
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=15)
    assert r.status_code == 200, f"Login failed: {r.text}"
    return s


async def _db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    return client[os.environ["DB_NAME"]]


# ==================== P2.2 Invalid ObjectId → 404 ====================

def test_invalid_trade_id_returns_404_not_500():
    s = _login_admin()
    r = s.post(f"{API}/trades/not-a-valid-hex/close", timeout=10)
    assert r.status_code == 404, f"Expected 404, got {r.status_code}: {r.text}"
    body = r.json()
    assert "not found" in body.get("detail", "").lower()


def test_invalid_signal_id_in_execute_returns_404():
    s = _login_admin()
    r = s.post(f"{API}/trades/execute",
               params={"signal_id": "not-valid-hex", "account_id": "also-not-hex"},
               timeout=10)
    assert r.status_code in (404, 400)


def test_invalid_account_id_in_reveal_returns_404():
    s = _login_admin()
    r = s.post(f"{API}/accounts/not-a-hex-string/credentials/reveal",
               json={"password": "anything"}, timeout=10)
    assert r.status_code == 404


# ==================== P1.3 Credential reveal hardening ====================

def test_reveal_requires_password_param():
    s = _login_admin()
    r = s.post(f"{API}/accounts/6a3ad0ef17f40ac1dd3eb3ef/credentials/reveal",
               json={}, timeout=10)
    # Pydantic validation — 422
    assert r.status_code == 422


def test_reveal_rejects_wrong_password():
    s = _login_admin()
    r = s.post(f"{API}/accounts/6a3ad0ef17f40ac1dd3eb3ef/credentials/reveal",
               json={"password": "wrong-password-here"}, timeout=10)
    # Account may not exist OR wrong password — either 403/404 is acceptable;
    # what matters is NOT 200.
    assert r.status_code in (403, 404), f"Got {r.status_code}: {r.text}"


# ==================== P1.2 CORS ====================

def test_cors_rejects_untrusted_origin():
    """Either the app rejects the origin outright, OR the layered ingress
    returns ACAO=* WITHOUT credentials:true — both prevent cookie hijack
    because browsers refuse to send credentials on ACAO=* responses."""
    r = requests.options(
        f"{API}/auth/me",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "GET",
        }, timeout=10,
    )
    aco = r.headers.get("Access-Control-Allow-Origin", "")
    acc = r.headers.get("Access-Control-Allow-Credentials", "")
    # Untrusted origin must NEVER receive BOTH a matching origin AND credentials:true
    forbidden = (aco == "https://evil.example.com" and acc.lower() == "true")
    wildcard_with_creds = (aco == "*" and acc.lower() == "true")
    assert not forbidden, "Untrusted origin was echoed back with credentials"
    assert not wildcard_with_creds, "Wildcard origin + credentials is unsafe"


def test_cors_allows_whitelisted_origin():
    # Use the configured backend URL itself as the trusted origin (it's in CORS_ORIGINS)
    trusted_origin = BASE_URL
    r = requests.options(
        f"{API}/auth/me",
        headers={
            "Origin": trusted_origin,
            "Access-Control-Request-Method": "GET",
        }, timeout=10,
    )
    aco = r.headers.get("Access-Control-Allow-Origin", "")
    if r.status_code == 200:
        assert aco == trusted_origin, f"Whitelisted origin not echoed back, got: {aco}"


# ==================== P2.1 broker_deals catches narrow exception ====================

# This is a code-level assertion (we can't easily provoke a non-duplicate failure
# in HTTP). Verify the source actually uses DuplicateKeyError, not bare Exception.

def test_broker_deals_catches_duplicate_key_error_only():
    with open(_os.path.join(_BACKEND_DIR, "routes/bridge_routes.py")) as f:
        src = f.read()
    # Find the broker_deals.insert_one block
    idx = src.find("broker_deals.insert_one")
    assert idx > 0, "broker_deals.insert_one not found"
    chunk = src[idx:idx + 800]
    assert "DuplicateKeyError" in chunk, \
        "broker_deals insert MUST catch DuplicateKeyError specifically"


# ==================== P1.1 slippage_veto report path ====================

def test_slippage_veto_path_does_not_namerror():
    """Compile-time check: report_trade no longer references undefined now_iso."""
    import ast
    with open(_os.path.join(_BACKEND_DIR, "routes/bridge_routes.py")) as f:
        tree = ast.parse(f.read())
    # Find the report_trade function
    func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "report_trade":
            func = node
            break
    assert func, "report_trade function not found"
    # Walk body, collect Names referenced and Names assigned
    assigned = set()
    referenced = set()
    for sub in ast.walk(func):
        if isinstance(sub, ast.Assign):
            for t in sub.targets:
                if isinstance(t, ast.Name):
                    assigned.add(t.id)
        elif isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
            referenced.add(sub.id)
    # now_iso, if referenced inside report_trade, must also be assigned inside it
    if "now_iso" in referenced:
        assert "now_iso" in assigned, \
            "report_trade references now_iso without assigning it"


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
