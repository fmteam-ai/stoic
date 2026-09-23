"""Iter-69 HTTP integration tests — exercises the new fields exposed by
/api/signals/generate and the two /api/insights/weekly-digest variants
+ POST /api/insights/weekly-digest/email through the live backend."""
from __future__ import annotations
from live_target import ADMIN_EMAIL, ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import os
import requests
import pytest

from live_target import require_live_base_url
BASE_URL = require_live_base_url()
if not BASE_URL.startswith("http"):
    BASE_URL = "https://" + BASE_URL

pass  # ADMIN_EMAIL comes from live_target
pass  # ADMIN_PASSWORD comes from live_target
# Load REACT_APP_BACKEND_URL from frontend/.env if not in env
if "REACT_APP_BACKEND_URL" not in os.environ:
    try:
        with open(_os.path.join(_REPO_DIR, "frontend", ".env")) as f:
            for line in f:
                if line.startswith("REACT_APP_BACKEND_URL"):
                    BASE_URL = line.split("=", 1)[1].strip().strip('"').rstrip("/")
    except Exception:
        pass


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
               timeout=15)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return s


# ─────────── /api/signals/generate ───────────
@pytest.mark.parametrize("symbol", ["XAUUSD", "BTCUSD"])
def test_signals_generate_has_breakout_and_vwap(admin_session, symbol):
    r = admin_session.post(f"{BASE_URL}/api/signals/generate",
                           json={"symbol": symbol, "risk_level": "medium"},
                           timeout=60)
    assert r.status_code == 200, f"{symbol}: {r.status_code} {r.text[:300]}"
    data = r.json()
    # The endpoint returns the AI dict or wraps it; locate AI block.
    ai = data.get("ai") if isinstance(data.get("ai"), dict) else data

    assert "breakout_scalper" in ai, f"breakout_scalper missing in {symbol} payload: keys={list(ai.keys())[:30]}"
    bs = ai["breakout_scalper"]
    for k in ("signal", "channel_high", "channel_low",
              "channel_width_pct", "atr", "break_distance_atr", "ready"):
        assert k in bs, f"{symbol} breakout_scalper missing key {k}: {bs}"

    assert "vwap_pullback" in ai, f"vwap_pullback missing in {symbol} payload"
    vw = ai["vwap_pullback"]
    for k in ("vwap", "pullback_pct", "above_vwap", "regime",
              "pullback_signal", "ready"):
        assert k in vw, f"{symbol} vwap_pullback missing key {k}: {vw}"


# ─────────── /api/insights/weekly-digest ───────────
def test_weekly_digest_no_ai_by_default(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/insights/weekly-digest", timeout=20)
    assert r.status_code == 200, r.text[:300]
    data = r.json()
    assert "ai_reflection" not in data, (
        f"ai_reflection should be absent without include_ai=true; got keys={list(data.keys())}"
    )


def test_weekly_digest_with_ai(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/insights/weekly-digest",
                          params={"include_ai": "true"}, timeout=90)
    assert r.status_code == 200, r.text[:300]
    data = r.json()
    assert "ai_reflection" in data, f"ai_reflection missing: keys={list(data.keys())}"
    refl = data["ai_reflection"]
    assert isinstance(refl, str), f"ai_reflection not a string: {type(refl)}"
    assert len(refl) >= 300, f"ai_reflection too short ({len(refl)} chars): {refl[:200]}"


# ─────────── POST /api/insights/weekly-digest/email ───────────
def test_weekly_digest_email_endpoint_shape(admin_session):
    r = admin_session.post(f"{BASE_URL}/api/insights/weekly-digest/email", timeout=60)
    assert r.status_code == 200, f"{r.status_code} {r.text[:300]}"
    data = r.json()
    # Shape assertions
    assert "configured" in data, f"missing 'configured': {data}"
    assert "recipient" in data, f"missing 'recipient': {data}"
    assert data["configured"] is True, f"expected configured:true (RESEND_API_KEY set), got {data}"
    # Either ok:true with id OR ok:false with error
    assert "ok" in data
    if data["ok"]:
        assert "id" in data, f"ok:true should include id: {data}"
    else:
        assert "error" in data, f"ok:false should include error: {data}"


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
