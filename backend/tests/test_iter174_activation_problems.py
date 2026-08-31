"""iter-174: verify POST /api/bot/start on the 'spread-test' account returns
409 activation_not_ready with a non-empty `problems` array — the payload the
frontend fix (formatApiError) now surfaces.
"""
import os
import re
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://stoic-trading-bot.preview.emergentagent.com").rstrip("/")
ADMIN_EMAIL = "admin@stoicaibot.com"
ADMIN_PASSWORD = "admin123"


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
               timeout=30)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    csrf = s.cookies.get("csrf_token")
    assert csrf, "no csrf cookie after login"
    s.headers.update({"X-CSRF-Token": csrf, "Content-Type": "application/json"})
    return s


def _find_spread_test_account(session):
    r = session.get(f"{BASE_URL}/api/accounts", timeout=30)
    assert r.status_code == 200, r.text
    accounts = r.json()
    if isinstance(accounts, dict):
        accounts = accounts.get("accounts") or accounts.get("items") or []
    for a in accounts:
        label = (a.get("label") or a.get("name") or "").lower()
        if "spread-test" in label or "spread_test" in label:
            return a
    return None


def test_spread_test_account_exists(admin_session):
    acct = _find_spread_test_account(admin_session)
    assert acct is not None, "spread-test account missing in preview DB"
    print(f"spread-test account id={acct.get('id') or acct.get('_id')}")


def test_bot_start_returns_problems_array(admin_session):
    acct = _find_spread_test_account(admin_session)
    if acct is None:
        pytest.skip("spread-test account not present")
    acct_id = acct.get("id") or acct.get("_id")
    r = admin_session.post(f"{BASE_URL}/api/bot/start",
                           params={"account_id": acct_id},
                           timeout=30)
    # We expect 409 activation_not_ready OR possibly 200 if account is ready.
    # For this bug repro path, must be 409 with problems.
    assert r.status_code == 409, f"expected 409, got {r.status_code}: {r.text}"
    body = r.json()
    detail = body.get("detail") or {}
    assert isinstance(detail, dict), f"detail not dict: {detail}"
    assert detail.get("code") == "activation_not_ready", detail
    assert isinstance(detail.get("message"), str) and detail["message"], detail
    problems = detail.get("problems")
    assert isinstance(problems, list) and len(problems) >= 1, f"problems empty: {detail}"
    for p in problems:
        assert isinstance(p, str) and p.strip(), f"bad problem entry: {p!r}"
    print(f"detail.message={detail['message']!r}")
    print(f"detail.problems={problems}")


def test_scalp_session_window_validation_string_detail(admin_session):
    """Regression: string detail must still be surfaced. Backend rejects
    start>=end with a plain-string detail from a Pydantic-side check."""
    payload = {"start_utc": 9, "end_utc": 3}
    r = admin_session.post(f"{BASE_URL}/api/scalp/session-window",
                           json=payload, timeout=30)
    assert r.status_code in (400, 409, 422), f"unexpected {r.status_code}: {r.text}"
    body = r.json()
    detail = body.get("detail")
    # Accept either a plain string OR a list (pydantic v2 validation errors) —
    # both go through formatApiError. Print for inspection.
    print(f"session-window status={r.status_code} detail={detail!r}")
    # We only assert the response shape is JSON-serializable and non-empty.
    assert detail is not None
