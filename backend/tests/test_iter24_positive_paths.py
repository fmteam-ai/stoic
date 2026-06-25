"""Iter24 positive-path verifications — happy paths must still work after the
ObjectId hardening migration."""
import os
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "http://localhost:3000").rstrip("/")
API = f"{BASE_URL}/api"
ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PASSWORD = "admin123"
VALID_ACCOUNT_HEX = "6a3ad0ef17f40ac1dd3eb3ef"


def _login_admin():
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=15)
    assert r.status_code == 200, f"Login failed: {r.text}"
    return s


def test_bot_config_no_account_id_returns_200():
    s = _login_admin()
    r = s.get(f"{API}/bot/config", timeout=10)
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
    data = r.json()
    # Defaults to scope=default
    assert isinstance(data, dict)


def test_bot_config_valid_account_id_returns_200():
    s = _login_admin()
    r = s.get(f"{API}/bot/config", params={"account_id": VALID_ACCOUNT_HEX}, timeout=10)
    # 200 if cfg exists/auto-created, 404 if account not found, but NOT 500
    assert r.status_code in (200, 404), f"Got {r.status_code}: {r.text}"


def test_bot_status_no_account_returns_200_no_crash():
    s = _login_admin()
    r = s.get(f"{API}/bot/status", timeout=15)
    assert r.status_code == 200, f"Got {r.status_code}: {r.text}"
    data = r.json()
    # Should have status field and not crash on corrupt configs
    assert isinstance(data, dict)


def test_nl_delete_valid_hex_id_returns_200():
    """Even non-existent valid hex id should not 500."""
    s = _login_admin()
    # Use a syntactically valid but non-existent hex id
    r = s.delete(f"{API}/nl/triggers/507f1f77bcf86cd799439011", timeout=10)
    # Either 200 ok (deleted/no-op) or 404 (not found) — must NOT be 500
    assert r.status_code in (200, 404), f"Got {r.status_code}: {r.text}"


def test_signals_delete_valid_hex_id_no_crash():
    s = _login_admin()
    r = s.delete(f"{API}/signals/507f1f77bcf86cd799439011", timeout=10)
    assert r.status_code in (200, 404), f"Got {r.status_code}: {r.text}"


def test_admin_payout_process_valid_hex_no_crash():
    s = _login_admin()
    r = s.post(f"{API}/admin/affiliate/payout-requests/507f1f77bcf86cd799439011/process",
               timeout=10)
    # Should be 404 (not found) since hex doesn't match a real record — not 500
    assert r.status_code in (200, 404, 400), f"Got {r.status_code}: {r.text}"


def test_admin_mark_paid_valid_hex_no_crash():
    s = _login_admin()
    r = s.post(f"{API}/admin/affiliate/commissions/507f1f77bcf86cd799439011/mark-paid",
               timeout=10)
    assert r.status_code in (200, 404, 400), f"Got {r.status_code}: {r.text}"
