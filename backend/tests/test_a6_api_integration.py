"""HTTP integration tests for A6 admin position-mode endpoints and ops panels.

Requires STOIC_TESTS_ALLOW_DB_NAME=1 since we hit preview backend (DB ai_trading_bot).
"""
import uuid
import pytest
import requests

from live_target import admin_credentials, require_live_base_url  # noqa: E402

BASE_URL = require_live_base_url().rstrip("/")
# credentials come from the environment ONLY (TEST_ADMIN_EMAIL / TEST_ADMIN_PASSWORD) — never at rest
ADMIN_EMAIL, ADMIN_PASSWORD = admin_credentials(strict=False)
if not ADMIN_PASSWORD:
    pytest.skip("TEST_ADMIN_PASSWORD not in env", allow_module_level=True)


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=30)
    assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text[:300]}"
    csrf = s.cookies.get("csrf_token")
    if csrf:
        s.headers.update({"X-CSRF-Token": csrf, "x-csrf-token": csrf})
    return s


@pytest.fixture(scope="module")
def throwaway_account(admin_session):
    """Create a throwaway test account for the admin user via POST /api/accounts; cleaned up at module teardown.
    Falls back to picking a stable test account from GET /api/admin/account-position-modes if creation is blocked.
    """
    nickname = f"TEST_A6_{uuid.uuid4().hex[:8]}"
    acc_num = str(900_000_000 + int(uuid.uuid4().int % 10_000_000))
    payload = {
        "label": nickname,
        "account_number": acc_num,
        "broker": "IC Markets",
        "server": "ICMarkets-Demo02",
        "balance": 10000,
        "currency": "USD",
        "nickname": nickname,
        "account_type": "demo",
    }
    r = admin_session.post(f"{BASE_URL}/api/accounts", json=payload, timeout=30)
    created_id = None
    if r.status_code in (200, 201):
        acc = r.json()
        created_id = acc.get("id") or acc.get("account_id") or acc.get("_id")
    if created_id:
        yield {"id": created_id, "account_number": acc_num, "created": True}
        try:
            admin_session.delete(f"{BASE_URL}/api/accounts/{created_id}", timeout=15)
        except Exception:
            pass
        return
    # Fallback: use an existing account from the position-modes list
    rr = admin_session.get(f"{BASE_URL}/api/admin/account-position-modes", timeout=30)
    accs = rr.json().get("accounts", []) if rr.status_code == 200 else []
    assert accs, f"could not create account ({r.status_code}) and no existing accounts in list: {r.text[:200]}"
    acc_id = accs[0]["account_id"]
    yield {"id": acc_id, "account_number": accs[0].get("account_number"), "created": False}
    # Reset mode to auto on exit
    try:
        admin_session.post(
            f"{BASE_URL}/api/admin/account-position-modes/{acc_id}",
            json={"mode": "auto", "password": ADMIN_PASSWORD, "reason": "qa-teardown"},
            timeout=20,
        )
    except Exception:
        pass


def _find_row(resp_json, account_id):
    for row in resp_json.get("accounts", []):
        if str(row.get("account_id")) == str(account_id):
            return row
    return None


def test_list_account_position_modes(admin_session, throwaway_account):
    r = admin_session.get(f"{BASE_URL}/api/admin/account-position-modes", timeout=30)
    assert r.status_code == 200, r.text[:400]
    data = r.json()
    assert "accounts" in data
    assert isinstance(data["accounts"], list)
    # Row shape check on first row
    if data["accounts"]:
        row = data["accounts"][0]
        for k in ("account_id", "mode", "source"):
            assert k in row, f"row missing {k}: {row}"
        assert row["mode"] in ("netting", "hedging")
        assert row["source"] in ("admin", "ea", "registry", "default")


def test_set_mode_invalid_value_422(admin_session, throwaway_account):
    r = admin_session.post(
        f"{BASE_URL}/api/admin/account-position-modes/{throwaway_account['id']}",
        json={"mode": "weird", "password": ADMIN_PASSWORD, "reason": "qa"},
        timeout=30,
    )
    assert r.status_code in (400, 422), f"expected 422 got {r.status_code}: {r.text[:300]}"


def test_set_mode_wrong_password_401(admin_session, throwaway_account):
    r = admin_session.post(
        f"{BASE_URL}/api/admin/account-position-modes/{throwaway_account['id']}",
        json={"mode": "netting", "password": "wrong-pw-xxx", "reason": "qa"},
        timeout=30,
    )
    assert r.status_code in (401, 403), f"expected 401/403 got {r.status_code}: {r.text[:300]}"


def test_set_mode_netting_then_auto(admin_session, throwaway_account):
    acc_id = throwaway_account["id"]
    # Set to netting
    r = admin_session.post(
        f"{BASE_URL}/api/admin/account-position-modes/{acc_id}",
        json={"mode": "netting", "password": ADMIN_PASSWORD, "reason": "qa"},
        timeout=30,
    )
    assert r.status_code == 200, r.text[:400]
    # Verify in list
    r2 = admin_session.get(f"{BASE_URL}/api/admin/account-position-modes", timeout=30)
    row = _find_row(r2.json(), acc_id)
    assert row is not None, "throwaway account missing from list"
    assert row["mode"] == "netting"
    assert row["source"] == "admin"
    override = row.get("override") or {}
    assert str(override.get("by") or "").lower() == ADMIN_EMAIL.lower()

    # Clear back to auto
    r3 = admin_session.post(
        f"{BASE_URL}/api/admin/account-position-modes/{acc_id}",
        json={"mode": "auto", "password": ADMIN_PASSWORD, "reason": "qa-reset"},
        timeout=30,
    )
    assert r3.status_code == 200, r3.text[:400]
    r4 = admin_session.get(f"{BASE_URL}/api/admin/account-position-modes", timeout=30)
    row2 = _find_row(r4.json(), acc_id)
    assert row2 is not None
    assert row2["source"] in ("registry", "default")


def test_accounts_list_exposes_position_mode_fields(admin_session, throwaway_account):
    r = admin_session.get(f"{BASE_URL}/api/accounts", timeout=30)
    assert r.status_code == 200
    accs = r.json()
    if isinstance(accs, dict):
        accs = accs.get("accounts", [])
    non_paper = [a for a in accs if a.get("account_type") != "paper"]
    assert non_paper, "no non-paper accounts in list"
    sample = non_paper[0]
    assert "position_mode" in sample
    assert "position_mode_source" in sample


def test_ops_console_unique_ticket_index(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/admin/ops-console", timeout=30)
    assert r.status_code == 200, r.text[:400]
    data = r.json()
    uti = data.get("unique_ticket_index")
    assert uti is not None, f"ops-console missing unique_ticket_index: keys={list(data)[:20]}"
    for k in ("ok", "present", "stale", "checked_at"):
        assert k in uti, f"unique_ticket_index missing {k}: {uti}"


def test_release_readiness_unique_ticket_index(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/ops/release-readiness", timeout=30)
    # Endpoint returns 503 when overall readiness != ok, but checks dict is still present
    assert r.status_code in (200, 503), r.text[:400]
    checks = r.json().get("checks", {})
    uti = checks.get("unique_ticket_index")
    assert uti is not None, "release-readiness missing checks.unique_ticket_index"
    assert uti.get("ok") is True, f"unique_ticket_index not ok: {uti}"
