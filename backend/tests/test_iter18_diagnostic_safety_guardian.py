"""Iter18 regression — verify /api/diagnostic/run surfaces Safety Guardian checks.

Validates:
  - Admin login succeeds (cookie-based auth).
  - GET /api/diagnostic/run returns Risk State section with both
    `Safety Guardian active` (showing thresholds) and
    `Safety Guardian recent blocks (24h)` checks.
  - Non-admin user gets 403.
"""
import os
import uuid
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    try:
        with open("/app/frontend/.env") as f:
            for line in f:
                if line.startswith("REACT_APP_BACKEND_URL="):
                    BASE_URL = line.split("=", 1)[1].strip().rstrip("/")
    except Exception:
        pass

ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PASSWORD = "admin123"


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
               timeout=15)
    if r.status_code != 200:
        pytest.skip(f"Admin login failed: {r.status_code} {r.text[:200]}")
    return s


@pytest.fixture(scope="module")
def user_session():
    from helpers import register_and_login
    email = f"TEST_iter18_safety_{uuid.uuid4().hex[:8]}@example.com"
    try:
        return register_and_login(email, "Passw0rd!", name="T18")
    except AssertionError as e:
        pytest.skip(f"User register/login failed: {e}")


def _flatten_checks(data: dict) -> list:
    """Tolerate either {sections:[{checks:[...]}]} or {checks:[...]} shape."""
    all_checks = []
    sections = data.get("sections") or data.get("results") or []
    if isinstance(sections, list):
        for sec in sections:
            if isinstance(sec, dict):
                all_checks.extend(sec.get("checks") or [])
    if not all_checks and isinstance(data.get("checks"), list):
        all_checks = data["checks"]
    return all_checks


def test_diagnostic_run_admin_returns_safety_guardian_checks(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/diagnostic/run", timeout=30)
    assert r.status_code == 200, f"diagnostic/run failed: {r.status_code} {r.text[:500]}"
    data = r.json()
    all_checks = _flatten_checks(data)
    assert all_checks, f"No checks in response: keys={list(data.keys())} body={str(data)[:400]}"

    def _name(c):
        return c.get("label") or c.get("name") or c.get("title")
    names = [_name(c) for c in all_checks]
    assert "Safety Guardian active" in names, f"missing 'Safety Guardian active' in {names}"
    assert "Safety Guardian recent blocks (24h)" in names, (
        f"missing 'Safety Guardian recent blocks (24h)' in {names}"
    )

    active = next(c for c in all_checks if _name(c) == "Safety Guardian active")
    detail = active.get("detail") or active.get("message") or ""
    assert "Per-trade risk" in detail, f"detail missing per-trade risk: {detail}"
    assert "Daily loss" in detail, f"detail missing daily loss: {detail}"
    assert "Aggregate risk" in detail, f"detail missing aggregate risk: {detail}"
    # Numeric thresholds present
    assert "3" in detail and "6" in detail and "9" in detail, f"detail missing numeric thresholds: {detail}"
    assert active.get("status") == "pass", f"Active check not pass: {active}"

    recent = next(c for c in all_checks
                  if _name(c) == "Safety Guardian recent blocks (24h)")
    assert recent.get("status") in ("pass", "warn"), f"Unexpected status: {recent}"


def test_diagnostic_run_non_admin_returns_403(user_session):
    r = user_session.get(f"{BASE_URL}/api/diagnostic/run", timeout=15)
    assert r.status_code == 403, f"expected 403, got {r.status_code} {r.text[:200]}"
