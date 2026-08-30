"""iter-44 · Trade History endpoint — /api/trades/history."""
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
_FRONT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "frontend", ".env")
if os.path.exists(_FRONT):
    load_dotenv(_FRONT, override=False)
from live_target import require_live_base_url
API = require_live_base_url() + "/api"


def _session():
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": "admin@trading.bot", "password": "admin123"}, timeout=30)
    assert r.status_code == 200, r.text
    return s


def test_history_shape_and_summary_math():
    s = _session()
    r = s.get(f"{API}/trades/history?date_from=2026-07-01&date_to=2026-07-04", timeout=30)
    assert r.status_code == 200, r.text
    d = r.json()
    assert "summary" in d and "trades" in d
    su = d["summary"]
    for k in ("total_trades", "closed_trades", "open_trades", "wins", "losses",
              "breakeven", "win_rate", "total_pnl", "avg_win", "avg_loss"):
        assert k in su, f"missing {k}"
    # Summary must agree with the returned trades
    closed = [t for t in d["trades"] if t.get("status") == "closed"]
    wins = [t for t in closed if float(t.get("pnl") or 0) > 0]
    assert su["closed_trades"] == len(closed)
    assert su["wins"] == len(wins)
    assert su["wins"] + su["losses"] + su["breakeven"] == su["closed_trades"]
    if closed:
        assert su["win_rate"] == round(len(wins) / len(closed) * 100, 1)
        assert abs(su["total_pnl"] - sum(float(t.get("pnl") or 0) for t in closed)) < 0.05
    # No 100-row cap
    assert su["total_trades"] == len(d["trades"])


def test_history_validation():
    s = _session()
    assert s.get(f"{API}/trades/history?date_from=bad&date_to=2026-07-04",
                 timeout=15).status_code == 400
    assert s.get(f"{API}/trades/history?date_from=2026-07-04&date_to=2026-07-01",
                 timeout=15).status_code == 400
    assert s.get(f"{API}/trades/history?date_from=2026-07-01",
                 timeout=15).status_code == 422  # date_to required


def test_history_empty_range():
    s = _session()
    r = s.get(f"{API}/trades/history?date_from=2001-01-01&date_to=2001-01-02", timeout=15)
    assert r.status_code == 200
    d = r.json()
    assert d["trades"] == [] and d["summary"]["win_rate"] == 0.0


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
