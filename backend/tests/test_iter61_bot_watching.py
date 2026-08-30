"""HTTP tests for the Bot Watching widget (iter-61).

Verifies GET /api/signals/watch-status — used by the
"Bot is patiently watching" dashboard tile.
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
from live_target import require_live_base_url

BASE_URL = require_live_base_url()

ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PASSWORD = "admin123"


@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    r = s.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=15,
    )
    assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text}"
    yield s


def test_watch_status_requires_auth():
    r = requests.get(f"{BASE_URL}/api/signals/watch-status", timeout=10)
    assert r.status_code == 401


def test_watch_status_returns_expected_shape(session):
    r = session.get(f"{BASE_URL}/api/signals/watch-status", timeout=10)
    assert r.status_code == 200, r.text
    body = r.json()

    # Top-level shape
    assert "symbols" in body and isinstance(body["symbols"], list)
    assert "cooldown_minutes" in body
    assert "hold_streak" in body
    assert "philosophy" in body and "STOIC" in body["philosophy"]

    # Per-symbol shape — when data is present
    for s in body["symbols"]:
        assert "symbol" in s
        if s.get("status") == "no_data":
            continue
        # Required fields on populated symbols
        for key in ("action", "entropy", "entropy_threshold", "noisy",
                    "reason", "sentiment", "indicators", "session",
                    "last_evaluated_at", "seconds_since_last_eval",
                    "next_evaluation_in_seconds"):
            assert key in s, f"missing key {key} for symbol {s['symbol']}"
        # Entropy threshold is a sensible probability
        assert 0.0 <= s["entropy_threshold"] <= 1.0
        # noisy boolean matches entropy vs threshold
        if s["entropy"] is not None:
            assert s["noisy"] == (s["entropy"] >= s["entropy_threshold"])


def test_watch_status_hold_streak_non_negative(session):
    r = session.get(f"{BASE_URL}/api/signals/watch-status", timeout=10)
    body = r.json()
    assert body["hold_streak"] >= 0


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
