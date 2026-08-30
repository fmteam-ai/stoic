"""iter-154 · Execution Observability UI backend guarantees:
   (1) GET /api/scalp/executions   (2) trades /audit execution_summary
   (3) certification new checks: order_check, fill_policy, symbol_mapping
   (4) step-up MFA gate regression on POST /api/bot/start"""
import os as _os
import requests
import pytest

_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)

from dotenv import load_dotenv  # noqa: E402
from live_target import require_live_base_url
load_dotenv(_os.path.join(_BACKEND_DIR, ".env"))

BASE = require_live_base_url()


def _login():
    s = requests.Session()
    r = s.post(f"{BASE}/api/auth/login",
               json={"email": "admin@trading.bot", "password": "admin123"},
               timeout=15)
    assert r.status_code == 200, r.text
    # attach csrf header from cookie for state-changing requests
    csrf = s.cookies.get("csrf_token")
    if csrf:
        s.headers.update({"X-CSRF-Token": csrf})
    return s


# ---------------- BACKEND 1: /api/scalp/executions
class TestScalpExecutions:
    def test_scalp_executions_shape(self):
        s = _login()
        r = s.get(f"{BASE}/api/scalp/executions?limit=5", timeout=20)
        assert r.status_code == 200, r.text
        j = r.json()
        assert "items" in j and "generated_at" in j
        assert isinstance(j["items"], list)
        assert isinstance(j["generated_at"], str) and len(j["generated_at"]) > 10

    def test_scalp_executions_bogus_account(self):
        s = _login()
        r = s.get(f"{BASE}/api/scalp/executions?limit=5&account_id=bogus-xyz",
                  timeout=20)
        assert r.status_code == 200
        assert r.json()["items"] == []


# ---------------- BACKEND 2: /api/trades/{id}/audit execution_summary
_TRADE_WITH_EVAL = "6a612a5b6936705cbd7c78f3"
_TRADE_MOSTLY_NULL = "6a61ae5f883889ba6a1a7c0a"
_REQUIRED_KEYS = {
    "entry_quality", "exit_quality", "mfe_r", "mae_r", "realized_r",
    "lesson", "slippage_pips", "expected_cost_pips",
    "commission", "swap", "reconciliation",
}
_RECON_KEYS = {"financial_status", "pnl_estimated", "pnl_unknown",
               "backfilled_at", "replayed_at"}


class TestTradeAuditExecutionSummary:
    def test_audit_populated_trade(self):
        s = _login()
        r = s.get(f"{BASE}/api/trades/{_TRADE_WITH_EVAL}/audit", timeout=20)
        assert r.status_code == 200, r.text
        es = r.json().get("execution_summary")
        assert es is not None
        assert set(es.keys()) >= _REQUIRED_KEYS
        # populated values from the eval seed
        assert es["entry_quality"] == 50
        assert es["exit_quality"] == 60
        assert es["commission"] == -0.68
        assert set(es["reconciliation"].keys()) >= _RECON_KEYS

    def test_audit_mostly_null_trade(self):
        s = _login()
        r = s.get(f"{BASE}/api/trades/{_TRADE_MOSTLY_NULL}/audit", timeout=20)
        assert r.status_code == 200, r.text
        es = r.json().get("execution_summary")
        assert es is not None
        assert set(es.keys()) >= _REQUIRED_KEYS
        assert es["entry_quality"] is None
        assert es["exit_quality"] is None
        assert es["expected_cost_pips"] == 5.25
        assert set(es["reconciliation"].keys()) >= _RECON_KEYS


# ---------------- BACKEND 3: certification new checks
class TestCertificationNewChecks:
    def test_cert_has_new_checks(self):
        s = _login()
        r = s.get(f"{BASE}/api/accounts/certification", timeout=20)
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) > 0
        keys = {c["key"] for c in items[0]["checks"]}
        assert "order_check" in keys
        assert "fill_policy" in keys
        assert "symbol_mapping" in keys
        # total 14 checks per acceptance criterion
        assert len(items[0]["checks"]) == 14


# ---------------- REGRESSION: step-up MFA gate still 403 for admin
class TestStepUpMFARegression:
    def test_bot_start_admin_no_token_returns_mfa_enrollment_required(self):
        s = _login()
        # Opt-out of conftest's X-Step-Up-Bypass so we hit the REAL gate.
        s.headers["X-Step-Up-Bypass"] = ""
        r = s.post(f"{BASE}/api/bot/start", json={}, timeout=15)
        assert r.status_code == 403, r.text
        detail = r.json().get("detail")
        assert isinstance(detail, dict)
        assert detail.get("code") == "mfa_enrollment_required"


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
