"""HTTP tests for iter-64 dashboard widgets:
  • Reachability indicator on /api/crypto/exchanges
  • Cooldown panel data — /api/bot/cooldowns
  • Risk-gauge data — /api/bot/risk-gauge
  • Weekly AI Digest — /api/insights/weekly-digest
"""
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import os
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    with open(_os.path.join(_REPO_DIR, "frontend", ".env")) as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = line.split("=", 1)[1].strip().rstrip("/")
                break

ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PASSWORD = "admin123"


@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=15)
    assert r.status_code == 200
    yield s


# ───────────────────── /bot/cooldowns ─────────────────────
def test_cooldowns_requires_auth():
    assert requests.get(f"{BASE_URL}/api/bot/cooldowns", timeout=10).status_code == 401


def test_cooldowns_shape(session):
    r = session.get(f"{BASE_URL}/api/bot/cooldowns", timeout=15)
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    for item in body["items"]:
        for key in ("config_id", "label", "broker", "active", "cooldown_minutes",
                    "symbols", "loss_streak", "win_streak", "anti_tilt_threshold",
                    "anti_tilt_active"):
            assert key in item, f"missing {key} on cooldown item"
        # symbols list embeds market closure data
        for s in item["symbols"]:
            assert "symbol" in s and "market_closed" in s


# ───────────────────── /bot/risk-gauge ─────────────────────
def test_risk_gauge_requires_auth():
    assert requests.get(f"{BASE_URL}/api/bot/risk-gauge", timeout=10).status_code == 401


def test_risk_gauge_shape(session):
    r = session.get(f"{BASE_URL}/api/bot/risk-gauge", timeout=15)
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    for item in body["items"]:
        for k in ("config_id", "label", "broker", "active", "tripped",
                  "equity", "daily", "weekly"):
            assert k in item, f"missing {k} on risk-gauge item"
        for window in ("daily", "weekly"):
            g = item[window]
            for k in ("enabled", "pnl", "limit_pct", "limit_amount", "consumed_pct"):
                assert k in g, f"missing {k} on {window} gauge"
            assert 0 <= g["consumed_pct"] <= 100


# ───────────────────── /insights/weekly-digest ─────────────────────
def test_weekly_digest_requires_auth():
    assert requests.get(f"{BASE_URL}/api/insights/weekly-digest", timeout=10).status_code == 401


def test_weekly_digest_shape(session):
    r = session.get(f"{BASE_URL}/api/insights/weekly-digest", timeout=15)
    assert r.status_code == 200
    body = r.json()
    for k in ("window_days", "window_start", "stats", "auto_heal_breakdown",
              "hold_reasons", "suggested_action", "generated_at"):
        assert k in body, f"missing {k} on digest"
    s = body["stats"]
    for k in ("trades", "wins", "losses", "win_rate", "pnl_total",
              "avg_win", "avg_loss", "auto_heals"):
        assert k in s, f"missing {k} on digest stats"
    assert 0 <= s["win_rate"] <= 100
    # Suggested action is a non-empty string
    assert isinstance(body["suggested_action"], str) and body["suggested_action"]


def test_weekly_digest_custom_window(session):
    r = session.get(f"{BASE_URL}/api/insights/weekly-digest?days=30", timeout=15)
    assert r.status_code == 200
    assert r.json()["window_days"] == 30


def test_weekly_digest_clamps_invalid_window(session):
    """days must be clamped to [1, 30]."""
    r = session.get(f"{BASE_URL}/api/insights/weekly-digest?days=500", timeout=15)
    assert r.status_code == 200
    assert r.json()["window_days"] == 30


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
