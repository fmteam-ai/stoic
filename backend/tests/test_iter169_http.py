"""HTTP-level audit v5 checks via public URL with admin cookie."""
import pytest
import requests

from live_target import require_live_base_url

pytestmark = pytest.mark.http

BASE_URL = require_live_base_url()


@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": "admin@trading.bot", "password": "admin123"},
               timeout=15)
    assert r.status_code == 200, r.text
    return s


def test_postmortem_guards_none_below_gate(session):
    r = session.get(f"{BASE_URL}/api/postmortem/guards", timeout=15)
    assert r.status_code == 200, r.text
    data = r.json()
    guards = data.get("guards") or data.get("active") or []
    if isinstance(data, dict) and "active" in data and isinstance(
            data["active"], list):
        guards = data["active"]
    active_bad = []
    xau_ny_active = None
    for g in guards:
        title = ((g.get("measure") or {}).get("title")) or g.get("title") or ""
        if g.get("active"):
            ev = g.get("latest_evidence") or g.get("evidence") or {}
            net = float(ev.get("net_effect") or 0)
            saved = float(ev.get("losses_avoided") or 0)
            missed = float(ev.get("wins_missed") or 0)
            if net < 100 or saved < 2 * missed:
                active_bad.append((title, net, saved, missed))
            if "XAUUSD" in title and "NY" in title:
                xau_ny_active = g
    assert active_bad == [], f"guards active below gate: {active_bad}"
    # xau ny may live under a history block - accept absent-from-active
    assert xau_ny_active is None, (
        "Suspend XAUUSD BUY in NY Session should not be active anymore")


def test_verified_performance_environment_present(session):
    r = session.get(f"{BASE_URL}/api/performance/verified", timeout=15)
    assert r.status_code == 200, r.text
    payload = r.json()
    accounts = payload.get("accounts") or []
    assert accounts, "expected account rows"
    for row in accounts:
        env = row.get("environment")
        assert env in ("DEMO", "PAPER", "UNKNOWN", "LIVE"), row
        assert env != "LIVE", f"preview should have no LIVE rows: {row}"


def test_chaos_drills_per_status(session):
    r = session.post(f"{BASE_URL}/api/ops/chaos/run", timeout=30)
    assert r.status_code in (200, 202), r.text
    r2 = session.get(f"{BASE_URL}/api/ops/chaos", timeout=15)
    assert r2.status_code == 200, r2.text
    payload = r2.json()
    results = payload.get("results") or payload.get("last", {}).get(
        "results") or []
    assert results, f"no chaos results: {payload}"
    for res in results:
        assert res.get("status") in ("PASS", "PARTIAL", "FAIL"), res
