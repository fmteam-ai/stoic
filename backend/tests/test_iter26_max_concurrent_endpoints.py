"""Iter 26 endpoint regression — wraps the unit-level max_concurrent race fix
with API-level regression checks for the new diagnostic + status surfaces.

Covers:
  * /api/bot/status (admin, no account_id) — must auto-select per-account cfg
  * /api/diagnostic/run — admin only, returns 6 sections
  * /api/diagnostic/auto-fix — valid codes return result dicts, unknown
    code returns {"error": "unknown fix code"}, non-admin = 403
  * /api/bot/health-score — anti-tilt freeze surfaces as code='anti_tilt_freeze'
  * PaperEngine cap guard (unit-level supplement to the MT5 race tests)
"""
import os
import uuid
import pytest
import requests
from unittest.mock import AsyncMock, MagicMock


@pytest.fixture(autouse=True)
def _market_always_open(monkeypatch):
    """Cap-logic tests must not depend on the wall-clock trading session."""
    monkeypatch.setattr("microstructure.is_market_closed", lambda s: None)

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL",
                          "https://stoic-trading.preview.emergentagent.com").rstrip("/")
ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PASS = "admin123"


# ---------- shared fixtures --------------------------------------------------
@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASS}, timeout=15)
    if r.status_code != 200:
        pytest.skip(f"Admin login failed: {r.status_code} {r.text[:120]}")
    return s


@pytest.fixture(scope="module")
def user_session():
    """Fresh non-admin user — needed for 403 path on admin-only endpoints."""
    from helpers import register_and_login
    email = f"TEST_iter26_{uuid.uuid4().hex[:8]}@example.com"
    try:
        return register_and_login(email, "tester1234", name="T26")
    except AssertionError as e:
        pytest.skip(f"User register/login failed: {e}")


# ---------- /api/bot/status auto-select -------------------------------------
class TestBotStatus:
    def test_status_no_account_returns_scope_fields(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/bot/status", timeout=15)
        assert r.status_code == 200, r.text
        data = r.json()
        # New fields from this iteration
        assert "scope_account_id" in data, "response missing scope_account_id"
        assert "scope_auto_selected" in data, "response missing scope_auto_selected"
        assert "anti_tilt_frozen_until" in data, "response missing anti_tilt_frozen_until"
        # Standard fields preserved
        assert "active" in data
        assert "max_concurrent_trades" in data
        assert "open_trades" in data
        assert isinstance(data["open_trades"], int)

    def test_status_explicit_account_id_overrides_auto(self, admin_session):
        accs = admin_session.get(f"{BASE_URL}/api/accounts", timeout=15).json()
        if not accs:
            pytest.skip("no accounts on admin")
        acc_id = str(accs[0].get("id") or accs[0].get("_id"))
        r = admin_session.get(f"{BASE_URL}/api/bot/status?account_id={acc_id}", timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert data.get("scope_account_id") == acc_id
        assert data.get("scope_auto_selected") is False


# ---------- /api/diagnostic/run ---------------------------------------------
class TestDiagnosticRun:
    REQUIRED_SECTIONS = {
        "connectivity", "execution", "trade_sync",
        "risk_state", "ea_config", "broker_errors",
    }

    def test_admin_run_returns_all_sections(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/diagnostic/run", timeout=30)
        assert r.status_code == 200, r.text
        data = r.json()
        assert "sections" in data
        ids = {s["id"] for s in data["sections"]}
        missing = self.REQUIRED_SECTIONS - ids
        assert not missing, f"Missing diagnostic sections: {missing}"
        # Each section has a status + checks list
        for s in data["sections"]:
            assert s["status"] in ("pass", "warn", "fail"), s
            assert isinstance(s["checks"], list)
        assert data["status"] in ("pass", "warn", "fail")
        assert "auto_fixable_codes" in data
        assert isinstance(data["auto_fixable_codes"], list)

    def test_non_admin_run_403(self, user_session):
        r = user_session.get(f"{BASE_URL}/api/diagnostic/run", timeout=15)
        assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text[:160]}"


# ---------- /api/diagnostic/auto-fix ----------------------------------------
class TestDiagnosticAutoFix:
    def test_unknown_code_returns_error(self, admin_session):
        r = admin_session.post(f"{BASE_URL}/api/diagnostic/auto-fix",
                               json={"codes": ["no_such_code_xyz"]}, timeout=15)
        assert r.status_code == 200, r.text
        data = r.json()
        assert "results" in data
        assert data["results"].get("no_such_code_xyz") == {"error": "unknown fix code"}

    def test_valid_codes_return_result_dicts(self, admin_session):
        codes = ["ack_ghost_trades", "clear_stuck_modifications"]
        r = admin_session.post(f"{BASE_URL}/api/diagnostic/auto-fix",
                               json={"codes": codes}, timeout=20)
        assert r.status_code == 200, r.text
        data = r.json()
        for c in codes:
            assert c in data["results"], f"missing result for {c}"
            assert isinstance(data["results"][c], dict)
            # Should NOT have "error": "unknown fix code"
            assert data["results"][c].get("error") != "unknown fix code"

    def test_empty_codes_returns_400(self, admin_session):
        r = admin_session.post(f"{BASE_URL}/api/diagnostic/auto-fix",
                               json={"codes": []}, timeout=15)
        assert r.status_code == 400

    def test_non_admin_auto_fix_403(self, user_session):
        r = user_session.post(f"{BASE_URL}/api/diagnostic/auto-fix",
                              json={"codes": ["ack_ghost_trades"]}, timeout=15)
        assert r.status_code == 403


# ---------- /api/bot/health-score -------------------------------------------
class TestHealthScore:
    def test_admin_health_score_200(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/bot/health-score", timeout=15)
        assert r.status_code == 200, r.text
        data = r.json()
        # Health-score shape — score + issues list
        assert "score" in data or "health_score" in data, f"unexpected shape: {list(data)}"
        assert "issues" in data
        assert isinstance(data["issues"], list)
        # If anti_tilt_freeze is present, it should be a warning code
        for issue in data["issues"]:
            if issue.get("code") == "anti_tilt_freeze":
                assert issue.get("severity") == "warning", issue


# ---------- PaperEngine cap guard (unit) ------------------------------------
@pytest.mark.asyncio
async def test_paper_engine_blocks_when_cap_reached(monkeypatch):
    """Mirrors test_max_concurrent_race.py but for PaperEngine."""
    from execution import PaperEngine

    fake_db = MagicMock()
    fake_db.trades.count_documents = AsyncMock(return_value=5)
    fake_db.trades.insert_one = AsyncMock()
    monkeypatch.setattr("execution.get_db", lambda: fake_db)
    monkeypatch.setattr("execution.get_quote", AsyncMock(return_value={"price": 3960.0}))

    eng = PaperEngine()
    res = await eng.execute(
        user_id="u1",
        account={"_id": "a1", "broker": "PAPER", "mode": "paper"},
        signal={"symbol": "XAUUSD", "action": "BUY", "lot_size": 0.1,
                "entry_price": 3960.0, "stop_loss": 3950.0,
                "take_profit": 3980.0, "origin": "auto"},
        max_concurrent=5, cfg_account_id="acct_1",
    )
    assert res.get("blocked") == "max_concurrent_cap"
    fake_db.trades.insert_one.assert_not_called()


@pytest.mark.asyncio
async def test_paper_engine_no_cap_kwarg_skips_count(monkeypatch):
    """Backward-compat: manual paper trades without max_concurrent kwarg must
    never call count_documents (cap check is gated)."""
    from execution import PaperEngine

    fake_db = MagicMock()
    fake_db.trades.count_documents = AsyncMock(return_value=999)
    fake_inserted = MagicMock()
    fake_inserted.inserted_id = "fakeid"
    fake_db.trades.insert_one = AsyncMock(return_value=fake_inserted)
    monkeypatch.setattr("execution.get_db", lambda: fake_db)
    monkeypatch.setattr("execution.get_quote", AsyncMock(return_value={"price": 3960.0}))
    monkeypatch.setattr("execution.ws_manager.broadcast", AsyncMock())

    eng = PaperEngine()
    res = await eng.execute(
        user_id="u1",
        account={"_id": "a1", "broker": "PAPER", "mode": "paper"},
        signal={"symbol": "XAUUSD", "action": "BUY", "lot_size": 0.1,
                "entry_price": 3960.0, "stop_loss": 3950.0,
                "take_profit": 3980.0, "origin": "manual"},
    )
    assert "blocked" not in res
    fake_db.trades.count_documents.assert_not_called()
