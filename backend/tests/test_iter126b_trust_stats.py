"""iter-126 — landing trust bar public stats + alert-inbox stage criterion."""
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), ".env"))

from live_target import require_live_base_url
BASE = require_live_base_url()


def test_trust_stats_public_no_auth():
    r = requests.get(f"{BASE}/api/public/trust-stats", timeout=20)
    assert r.status_code == 200
    d = r.json()
    assert isinstance(d["accounts_protected"], int)
    assert isinstance(d["signals_vetoed"], int)
    assert d["accounts_protected"] >= 0 and d["signals_vetoed"] >= 0
    up = d["uptime_30d_pct"]
    assert up is None or (0.0 <= up <= 100.0)
    assert d["as_of"]


def test_trust_stats_no_per_user_data():
    d = requests.get(f"{BASE}/api/public/trust-stats", timeout=20).json()
    assert set(d.keys()) == {"accounts_protected", "signals_vetoed",
                             "uptime_30d_pct", "as_of"}


def test_alerts_endpoints_require_auth():
    assert requests.get(f"{BASE}/api/ops/alerts", timeout=20).status_code == 403
    assert requests.post(f"{BASE}/api/ops/alerts/ack-all",
                         timeout=20).status_code == 403


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
