"""HTTP tests for the Bot Watching widget (iter-61).

Verifies GET /api/signals/watch-status — used by the
"Bot is patiently watching" dashboard tile.
"""
import os
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    with open("/app/frontend/.env") as f:
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
