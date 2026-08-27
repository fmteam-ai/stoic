"""Iter24 — carry-over hardening of remaining raw ObjectId() calls.

Verifies that all migrated endpoints in bot/nl/signal/affiliate routes now
return 404 (not 500) when supplied with malformed ObjectId path/query params.
"""
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import os
import requests
import pytest

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "http://localhost:3000").rstrip("/")
API = f"{BASE_URL}/api"
ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PASSWORD = "admin123"


def _login_admin():
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=15)
    assert r.status_code == 200, f"Login failed: {r.text}"
    return s


# ==================== bot_routes.py ====================

def test_bot_sizing_preview_invalid_account_id_returns_404():
    s = _login_admin()
    r = s.get(f"{API}/bot/sizing-preview", params={"account_id": "garbage-id"}, timeout=10)
    assert r.status_code == 404, f"Expected 404, got {r.status_code}: {r.text}"


def test_bot_config_get_invalid_account_id_returns_404():
    s = _login_admin()
    r = s.get(f"{API}/bot/config", params={"account_id": "not-a-hex"}, timeout=10)
    assert r.status_code == 404


def test_bot_config_put_invalid_account_id_returns_404():
    s = _login_admin()
    r = s.put(f"{API}/bot/config", params={"account_id": "bad"},
              json={"risk_level": "medium"}, timeout=10)
    assert r.status_code == 404


def test_bot_start_invalid_account_id_returns_404():
    s = _login_admin()
    r = s.post(f"{API}/bot/start", params={"account_id": "not-hex"}, timeout=10)
    assert r.status_code == 404


def test_bot_preset_invalid_account_id_returns_404():
    s = _login_admin()
    r = s.post(f"{API}/bot/preset/conservative",
               params={"account_id": "bad-id"}, timeout=10)
    assert r.status_code == 404


# ==================== nl_routes.py ====================

def test_nl_delete_trigger_invalid_id_returns_404():
    s = _login_admin()
    r = s.delete(f"{API}/nl/triggers/not-a-hex-string", timeout=10)
    assert r.status_code == 404


# ==================== signal_routes.py ====================

def test_signals_delete_invalid_id_returns_404():
    s = _login_admin()
    r = s.delete(f"{API}/signals/not-a-hex-string", timeout=10)
    assert r.status_code == 404


# ==================== affiliate_routes.py (admin) ====================

def test_admin_payout_process_invalid_id_returns_404():
    s = _login_admin()
    r = s.post(f"{API}/admin/affiliate/payout-requests/not-hex/process", timeout=10)
    # Was 400 before — now 404 for consistency with other resources
    assert r.status_code == 404, f"Expected 404, got {r.status_code}: {r.text}"


def test_admin_mark_paid_invalid_id_returns_404():
    s = _login_admin()
    r = s.post(f"{API}/admin/affiliate/commissions/not-hex/mark-paid", timeout=10)
    assert r.status_code == 404


# ==================== verify no raw ObjectId() left in these files ====================

def test_no_unwrapped_object_id_calls_in_user_input_paths():
    """Source-level scan: every remaining ObjectId() call in the 4 hardened
    files must be either inside a try/except OR not on user-supplied input."""
    import re
    files = [
        _os.path.join(_BACKEND_DIR, "routes/bot_routes.py"),
        _os.path.join(_BACKEND_DIR, "routes/nl_routes.py"),
        _os.path.join(_BACKEND_DIR, "routes/signal_routes.py"),
        _os.path.join(_BACKEND_DIR, "routes/affiliate_routes.py"),
    ]
    pattern = re.compile(r"\bObjectId\(")
    for f in files:
        with open(f) as fh:
            src = fh.read()
        for match in pattern.finditer(src):
            start = match.start()
            # Find the surrounding 200 chars (line + a few before/after)
            line_start = src.rfind("\n", 0, start) + 1
            line_end = src.find("\n", start)
            line = src[line_start:line_end]
            # Look back ~300 chars for a `try:` block opening
            backctx = src[max(0, start - 400):start]
            has_try = "try:" in backctx
            # Names that come from request-supplied path/query params
            user_input_names = ("account_id", "trigger_id", "signal_id",
                                "trade_id", "rid", "cid")
            is_user_input = any(name + ")" in line or
                                f"({name})" in line.replace(" ", "") for name in user_input_names)
            assert not (is_user_input and not has_try), \
                f"{f}: line {src[:start].count(chr(10)) + 1} has unwrapped ObjectId() " \
                f"on user input: {line.strip()}"


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
