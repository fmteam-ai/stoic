import os as _os_p
_BACKEND_ROOT = _os_p.path.dirname(_os_p.path.dirname(_os_p.path.abspath(__file__)))
_ENV_BE = _os_p.path.join(_BACKEND_ROOT, ".env")
_ENV_FE = _os_p.path.join(_os_p.path.dirname(_BACKEND_ROOT), "frontend", ".env")
# v1.60.29 HTTP smoke: safety-status, trades/stats funnel, scalp status, account certification
import os
import pytest
import requests
from dotenv import dotenv_values

pytestmark = pytest.mark.http

_env = dotenv_values(_ENV_BE)
_fe = dotenv_values(_ENV_FE)
BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL") or _fe.get("REACT_APP_BACKEND_URL")).rstrip("/")
EMAIL = os.environ.get("TEST_ADMIN_EMAIL") or _env.get("TEST_ADMIN_EMAIL")
PASSWORD = os.environ.get("TEST_ADMIN_PASSWORD") or _env.get("TEST_ADMIN_PASSWORD")


@pytest.fixture(scope="module")
def client():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login", json={"email": EMAIL, "password": PASSWORD}, timeout=30)
    if r.status_code != 200:
        pytest.skip(f"login failed {r.status_code}")
    tok = (r.json() or {}).get("access_token") or (r.json() or {}).get("token")
    if tok:
        s.headers["Authorization"] = f"Bearer {tok}"
    return s


# Safety status: admin has no live accounts (DEMO attested), paper count present
def test_safety_status(client):
    r = client.get(f"{BASE_URL}/api/bot/safety-status", timeout=30)
    assert r.status_code == 200, r.text[:300]
    d = r.json()
    print("safety keys:", sorted(d.keys()))
    assert d.get("live_accounts") == [], d.get("live_accounts")
    assert d.get("deployment_mode") in ("IDLE", "PAPER", "LIVE", "MIXED")
    assert isinstance(d.get("paper_accounts"), int)


# Trade stats: funnel rows shape
def test_trade_stats_funnel(client):
    # funnel lives on /trades/scoreboard (what Scoreboard.jsx calls); /trades/stats has no funnel
    r = client.get(f"{BASE_URL}/api/trades/scoreboard", params={"days": 30}, timeout=60)
    assert r.status_code == 200, r.text[:300]
    funnel = r.json().get("funnel")
    assert isinstance(funnel, list)
    print("funnel rows:", len(funnel), "statuses:", sorted({f["status"] for f in funnel}))
    for row in funnel:
        for k in ("stage", "status", "count", "last_reason"):
            assert k in row, row
        assert isinstance(row["count"], int)


# Scalp status: requested_mode and live_gate per symbol
def test_scalp_status(client):
    r = client.get(f"{BASE_URL}/api/scalp/status", timeout=30)
    assert r.status_code == 200, r.text[:300]
    d = r.json()
    print("scalp status top keys:", list(d.keys())[:20] if isinstance(d, dict) else type(d))
    syms = d.get("runners") if isinstance(d, dict) else d
    print("runners:", syms if not syms else [(x.get("symbol"), x.get("mode"), x.get("requested_mode")) for x in (syms.values() if isinstance(syms, dict) else syms)])
    if isinstance(syms, dict):
        syms = list(syms.values())
    if not syms:
        pytest.skip("no scalp symbols configured")
    for s in syms:
        assert "requested_mode" in s, list(s.keys())
        assert "live_gate" in s, list(s.keys())


# Certification: EA version value is a string (version or em dash)
def test_certification(client):
    r = client.get(f"{BASE_URL}/api/accounts/certification", timeout=30)
    assert r.status_code == 200, r.text[:300]
    d = r.json()
    items = d.get("items", d) if isinstance(d, dict) else d
    assert isinstance(items, list)
    print("cert items:", len(items))
    for it in items:
        ea = [c for c in it.get("checks", []) if c.get("label") == "EA version"]
        assert ea, list(it.keys())
        assert isinstance(ea[0]["value"], str) and ea[0]["value"]
        print("EA version value:", ea[0]["value"])
