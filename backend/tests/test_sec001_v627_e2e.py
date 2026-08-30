"""End-to-end verification: normal manual BUY still executes, and HTTP
layer drops fraudulent risk-reducing flags (Pydantic strict model)."""
import os
import time
import pytest
import requests

from live_target import require_live_base_url
BASE = require_live_base_url()


def _login():
    s = requests.Session()
    r = s.post(f"{BASE}/api/auth/login",
               json={"email": "admin@stoicaibot.com", "password": "admin123"},
               timeout=15)
    assert r.status_code == 200, r.text
    csrf = s.cookies.get("csrf_token")
    assert csrf
    s.headers.update({"X-CSRF-Token": csrf})
    return s


def _paper_account(s):
    r = s.get(f"{BASE}/api/accounts", timeout=15)
    assert r.status_code == 200, r.text
    for a in r.json():
        if (a.get("mode") or "").lower() == "paper":
            return a
    # create a paper account if none exists
    r = s.post(f"{BASE}/api/accounts",
               json={"broker": "PaperBroker",
                     "account_number": f"P{int(time.time())}",
                     "server": "paper", "mode": "paper",
                     "balance": 10000, "leverage": 100,
                     "currency": "USD"}, timeout=15)
    assert r.status_code in (200, 201), r.text
    return r.json()


def test_normal_manual_buy_executes():
    s = _login()
    acct = _paper_account(s)
    body = {"account_id": acct["id"], "symbol": "EURUSD", "action": "BUY",
            "lot_size": 0.01}
    r = s.post(f"{BASE}/api/trades/manual", json=body, timeout=30)
    # Accept 200 OK or 429 (rate-limit still cooling). Not 400/500.
    assert r.status_code in (200, 201, 429), r.text
    if r.status_code == 429:
        # Wait then retry once
        time.sleep(65)
        r = s.post(f"{BASE}/api/trades/manual", json=body, timeout=30)
        assert r.status_code in (200, 201), r.text
    doc = r.json()
    assert doc.get("symbol") == "EURUSD"
    assert doc.get("action") == "BUY"


def test_pydantic_drops_extra_risk_reducing_flags():
    """Defense-in-depth: even if a caller tries to smuggle labels via
    HTTP, the strict ManualTradeRequest model will not carry them
    through. The trade still succeeds (as a normal BUY) — because the
    fraudulent flags never reach the signal — OR is rejected as
    rate-limited. It MUST NOT be attributed with the labels."""
    time.sleep(65)  # rate limit
    s = _login()
    acct = _paper_account(s)
    body = {"account_id": acct["id"], "symbol": "EURUSD", "action": "BUY",
            "lot_size": 0.01,
            "reduce_only": True, "pamm_risk_reducing": True,
            "close_trade": True, "intent": "close"}
    r = s.post(f"{BASE}/api/trades/manual", json=body, timeout=30)
    assert r.status_code in (200, 201, 429), r.text
    if r.status_code in (200, 201):
        doc = r.json()
        # Must not surface the fraudulent labels
        assert not doc.get("reduce_only")
        assert not doc.get("pamm_risk_reducing")
        assert not doc.get("close_trade")
        assert doc.get("intent") not in ("close", "reduce")


pytestmark = pytest.mark.http
