"""Tests for iter-65 daily profit target with lock-profit semantics."""
import os
import asyncio
from datetime import datetime, timezone

import pytest
import requests

from profit_target import (
    evaluate_profit_target, locked_profit_amount, _r_dollar_value,
)
from risk import get_profile, compute_lot_for_account
from circuit_breakers import today_iso

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    with open("/app/frontend/.env") as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = line.split("=", 1)[1].strip().rstrip("/")
                break

ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PASSWORD = "admin123"


# ──────────────────── pure helper tests ────────────────────
def test_r_dollar_value_matches_risk_pct_of_equity():
    # 1R = risk_pct × equity. Medium profile risk_pct = 1.0% by default
    profile = get_profile("medium")
    assert _r_dollar_value(10_000.0, profile) == pytest.approx(
        10_000.0 * profile["risk_pct"] / 100.0
    )


def test_r_dollar_value_zero_for_empty_equity():
    assert _r_dollar_value(0.0, get_profile("medium")) == 0.0
    assert _r_dollar_value(-100.0, get_profile("medium")) == 0.0


def test_locked_profit_subtracts_from_kelly_equity():
    """compute_lot_for_account must shrink lot when locked_profit > 0."""
    account = {"equity": 10_000.0, "account_type": "standard"}
    profile = get_profile("medium")
    base = compute_lot_for_account(
        account, "XAUUSD", entry_price=2000.0, stop_loss=1990.0,
        confidence_pct=80, profile=profile,
    )
    locked = compute_lot_for_account(
        account, "XAUUSD", entry_price=2000.0, stop_loss=1990.0,
        confidence_pct=80, profile=profile, locked_profit=5_000.0,
    )
    # 50% of equity locked → ~50% smaller lot
    assert locked["lot_size"] < base["lot_size"]
    assert locked["equity"] == pytest.approx(5_000.0)


def test_locked_profit_floor_at_zero():
    """Locked > equity must floor at $0 equity, not go negative."""
    account = {"equity": 1_000.0, "account_type": "standard"}
    out = compute_lot_for_account(
        account, "XAUUSD", entry_price=2000.0, stop_loss=1990.0,
        confidence_pct=80, profile=get_profile("medium"), locked_profit=99_999.0,
    )
    # 0 equity → C5 fail-closed rejection (never a tradeable 0.01)
    assert out["lot_size"] == 0.0
    assert out["sizing_valid"] is False
    assert out["method"] == "rejected_no_equity"


# ──────────────────── HTTP endpoint tests ────────────────────
@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    r = s.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=15,
    )
    assert r.status_code == 200
    yield s


def test_risk_gauge_includes_profit_target_block(session):
    r = session.get(f"{BASE_URL}/api/bot/risk-gauge", timeout=15)
    assert r.status_code == 200
    items = r.json()["items"]
    for it in items:
        assert "profit_target" in it
        pt = it["profit_target"]
        for k in ("enabled", "mode", "target_r", "target_amount",
                  "current_pnl", "r_dollar_value", "hit",
                  "locked_amount", "progress_pct"):
            assert k in pt, f"profit_target missing {k} for {it['label']}"
        assert pt["mode"] in {"lock", "stop"}
        assert 0 <= pt["progress_pct"] <= 100


def test_bot_config_accepts_profit_target_fields(session):
    r = session.put(
        f"{BASE_URL}/api/bot/config",
        json={"daily_profit_target_r": 3.5, "daily_profit_target_action": "stop"},
        timeout=15,
    )
    assert r.status_code == 200
    # Verify persisted via gauge endpoint
    g = session.get(f"{BASE_URL}/api/bot/risk-gauge", timeout=15).json()
    default = next((it for it in g["items"] if it["account_id"] is None), None)
    assert default is not None
    assert default["profit_target"]["target_r"] == 3.5
    assert default["profit_target"]["mode"] == "stop"
    # Cleanup — disable
    session.put(
        f"{BASE_URL}/api/bot/config",
        json={"daily_profit_target_r": 0, "daily_profit_target_action": "lock"},
        timeout=15,
    )


def test_profit_target_action_rejects_invalid(session):
    """Audit C1: a bogus action is rejected at the boundary (422), not
    silently coerced into the DB."""
    r = session.put(
        f"{BASE_URL}/api/bot/config",
        json={"daily_profit_target_r": 1, "daily_profit_target_action": "TURBO_LOCK"},
        timeout=15,
    )
    assert r.status_code == 422
    session.put(
        f"{BASE_URL}/api/bot/config",
        json={"daily_profit_target_r": 0}, timeout=15,
    )
