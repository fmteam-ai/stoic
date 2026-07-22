"""Iter 25e — Position Sizing Preview endpoint.

Returns the effective lot the bot would open at confidences 55-90% given
the user's actual account + risk profile + max_lot_size. Powers the
"Position Sizing Preview" panel on the BotConfig page so users can dial
in a sensible cap before risking real money.
"""
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import os
import pathlib
import pytest
import requests


_FRONT_ENV = pathlib.Path(_os.path.join(_REPO_DIR, "frontend", ".env"))


def _read_frontend_backend_url() -> str:
    if not _FRONT_ENV.exists():
        return ""
    for line in _FRONT_ENV.read_text().splitlines():
        if line.startswith("REACT_APP_BACKEND_URL="):
            return line.split("=", 1)[1].strip()
    return ""


BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL")
            or _read_frontend_backend_url()
            or "http://localhost:8001").rstrip("/")
API = f"{BASE_URL}/api"


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": "admin@trading.bot", "password": "admin123"},
               timeout=15)
    assert r.status_code == 200
    return s


class TestSizingPreview:
    def test_preview_returns_scaled_rows(self, admin_session):
        r = admin_session.get(f"{API}/bot/sizing-preview?symbol=XAUUSD", timeout=10)
        assert r.status_code == 200, r.text
        body = r.json()
        confs = [row["confidence_pct"] for row in body["rows"]]
        assert confs == [55, 60, 65, 70, 75, 80, 85, 90]
        # Effective lots must be monotonic non-decreasing
        lots = [row["effective_lot"] for row in body["rows"]]
        for prev, curr in zip(lots, lots[1:]):
            assert curr >= prev, lots
        # Sanity on context fields
        assert "account_label" in body
        assert "risk_level" in body
        assert "max_lot_size" in body

    def test_preview_default_symbol_is_xauusd(self, admin_session):
        r = admin_session.get(f"{API}/bot/sizing-preview", timeout=10)
        assert r.status_code == 200
        assert r.json()["symbol"] == "XAUUSD"

    def test_preview_btcusd_uses_btc_scenario(self, admin_session):
        r = admin_session.get(f"{API}/bot/sizing-preview?symbol=BTCUSD", timeout=10)
        assert r.status_code == 200
        body = r.json()
        assert body["scenario"]["entry_price"] == 62000.0
        assert body["scenario"]["stop_loss"] == 61500.0
