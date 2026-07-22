"""Iter-74 Adaptive Mode smoke test — end-to-end HTTP verification.

Covers the 6 backend smoke criteria from the review request:
  1. GET /api/bot/adaptive-status returns full shape for admin (default cfg)
  2. GET /api/bot/adaptive-status?account_id=… echoes account_id
  3. PUT /api/bot/config?account_id=… persists all 5 adaptive fields
  4. PUT with invalid profit_taking_mode coerces to 'expected_value'
  5. PUT with adaptive_risk_window=9999 clamps to 200
  6. GET /api/bot/presets includes fast_scalp preset

This file is restore-safe: it picks a NON-Tauro MT5 account for mutation
tests and restores it to the clean A/B baseline at the end.
"""
from __future__ import annotations
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import os
import pytest
import requests

# Load BASE_URL from frontend .env
BASE_URL = ""
try:
    with open(_os.path.join(_REPO_DIR, "frontend", ".env")) as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL"):
                BASE_URL = line.split("=", 1)[1].strip().strip('"').rstrip("/")
                break
except Exception:
    pass
assert BASE_URL, "REACT_APP_BACKEND_URL not configured"

ADMIN = {"email": "admin@trading.bot", "password": "admin123"}
TAURO_PREFIX = "6a41c0fc"  # protected — do not mutate

CLEAN_BASELINE = {
    "profit_taking_mode": "expected_value",
    "adaptive_risk_enabled": False,
    "auto_preset_enabled": False,
    "max_tp_pips_per_symbol": {},
    "adaptive_risk_window": 50,
}


# ───────────── fixtures ─────────────
@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login", json=ADMIN, timeout=15)
    assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text[:200]}"
    return s


@pytest.fixture(scope="module")
def accounts(session):
    r = session.get(f"{BASE_URL}/api/accounts", timeout=15)
    assert r.status_code == 200
    return r.json()


@pytest.fixture(scope="module")
def mt5_test_account(accounts):
    """Pick first MT5 (kind null/absent) account that is NOT Tauro for mutation."""
    for a in accounts:
        if a.get("kind"):
            continue
        if str(a.get("id", "")).startswith(TAURO_PREFIX):
            continue
        return a
    pytest.skip("no non-Tauro MT5 account available for mutation tests")


# ───────────── tests ─────────────

# ── adaptive-status shape ──
def test_adaptive_status_default_shape(session):
    r = session.get(f"{BASE_URL}/api/bot/adaptive-status", timeout=15)
    assert r.status_code == 200, r.text
    d = r.json()
    assert "profit_taking_mode" in d
    assert "max_tp_pips_per_symbol" in d and isinstance(d["max_tp_pips_per_symbol"], dict)
    assert "adaptive_risk" in d
    ar = d["adaptive_risk"]
    for k in ("enabled", "multiplier", "win_rate_pct", "samples"):
        assert k in ar, f"adaptive_risk missing {k}"
    assert "auto_preset" in d
    ap = d["auto_preset"]
    for k in ("enabled", "would_select", "reason"):
        assert k in ap, f"auto_preset missing {k}"
    assert "active_preset" in d


def test_adaptive_status_by_account_echoes_id(session, mt5_test_account):
    aid = mt5_test_account["id"]
    r = session.get(f"{BASE_URL}/api/bot/adaptive-status?account_id={aid}", timeout=15)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d.get("account_id") == aid, f"account_id not echoed: got {d.get('account_id')!r} expected {aid!r}"


# ── PUT /bot/config persistence ──
def test_put_bot_config_persists_all_five_adaptive_fields(session, mt5_test_account):
    aid = mt5_test_account["id"]
    payload = {
        "profit_taking_mode": "win_rate",
        "max_tp_pips_per_symbol": {"XAUUSD": 100, "BTCUSD": 100},
        "adaptive_risk_enabled": True,
        "adaptive_risk_window": 25,
        "auto_preset_enabled": True,
    }
    r = session.put(f"{BASE_URL}/api/bot/config?account_id={aid}", json=payload, timeout=15)
    assert r.status_code == 200, f"PUT failed: {r.status_code} {r.text[:400]}"

    r2 = session.get(f"{BASE_URL}/api/bot/config?account_id={aid}", timeout=15)
    assert r2.status_code == 200
    cfg = r2.json()
    assert cfg.get("profit_taking_mode") == "win_rate"
    assert cfg.get("max_tp_pips_per_symbol", {}).get("XAUUSD") == 100
    assert cfg.get("max_tp_pips_per_symbol", {}).get("BTCUSD") == 100
    assert cfg.get("adaptive_risk_enabled") is True
    assert cfg.get("adaptive_risk_window") == 25
    assert cfg.get("auto_preset_enabled") is True

    # restore baseline
    session.put(f"{BASE_URL}/api/bot/config?account_id={aid}", json=CLEAN_BASELINE, timeout=15)


def test_invalid_profit_taking_mode_coerces_to_expected_value(session, mt5_test_account):
    aid = mt5_test_account["id"]
    r = session.put(f"{BASE_URL}/api/bot/config?account_id={aid}",
                    json={"profit_taking_mode": "GARBAGE"}, timeout=15)
    assert r.status_code == 200, r.text
    r2 = session.get(f"{BASE_URL}/api/bot/config?account_id={aid}", timeout=15)
    assert r2.json().get("profit_taking_mode") == "expected_value"
    session.put(f"{BASE_URL}/api/bot/config?account_id={aid}", json=CLEAN_BASELINE, timeout=15)


def test_adaptive_risk_window_clamps_to_200(session, mt5_test_account):
    aid = mt5_test_account["id"]
    r = session.put(f"{BASE_URL}/api/bot/config?account_id={aid}",
                    json={"adaptive_risk_window": 9999}, timeout=15)
    assert r.status_code == 200, r.text
    r2 = session.get(f"{BASE_URL}/api/bot/config?account_id={aid}", timeout=15)
    assert r2.json().get("adaptive_risk_window") == 200
    session.put(f"{BASE_URL}/api/bot/config?account_id={aid}", json=CLEAN_BASELINE, timeout=15)


# ── presets ──
def test_fast_scalp_preset_present(session):
    r = session.get(f"{BASE_URL}/api/bot/presets", timeout=15)
    assert r.status_code == 200
    presets = r.json()
    items = presets if isinstance(presets, list) else (presets.get("presets") or presets.get("items") or [])
    keys = []
    labels = []
    for p in items:
        if isinstance(p, dict):
            keys.append(p.get("key") or p.get("id") or p.get("name"))
            labels.append(p.get("label") or p.get("name"))
    assert "fast_scalp" in keys, f"fast_scalp not in preset keys: {keys}"
    # label check (best-effort)
    fs = next((p for p in items if isinstance(p, dict) and (p.get("key") == "fast_scalp" or p.get("id") == "fast_scalp")), None)
    assert fs is not None
    assert (fs.get("label") or "").lower().startswith("fast"), f"label unexpected: {fs.get('label')}"
