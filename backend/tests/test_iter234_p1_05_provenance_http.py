"""HTTP smoke tests for iter234: P1-05/P2-01 chart provenance on
analytics attribution, safety-blocks stats, and trade replay endpoints."""
import os
import pytest
import requests

BASE = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
EMAIL = os.environ.get("TEST_ADMIN_EMAIL", "admin@trading.bot")
PASSWORD = os.environ.get("TEST_ADMIN_PASSWORD")


@pytest.fixture(scope="module")
def admin_session():
    if not PASSWORD:
        pytest.skip("TEST_ADMIN_PASSWORD missing")
    s = requests.Session()
    r = s.post(f"{BASE}/api/auth/login", json={"email": EMAIL, "password": PASSWORD}, timeout=20)
    assert r.status_code == 200, r.text
    csrf = s.cookies.get("csrf_token")
    assert csrf
    s.headers.update({"X-CSRF-Token": csrf})
    return s


def _assert_prov(p, kind, provider_eq=None, provider_prefix=None, note_contains=None):
    assert p["contract_version"] == 1
    assert p["source_kind"] == kind
    assert "chart_provenance.build" in (p.get("build") or p.get("builder") or "") or p.get("build") == "chart_provenance.build"
    if provider_eq:
        assert p["provider"] == provider_eq
    if provider_prefix:
        assert p["provider"].startswith(provider_prefix), p["provider"]
    if note_contains:
        assert note_contains.lower() in p.get("note", "").lower()


def test_analytics_attribution_provenance(admin_session):
    r = admin_session.get(f"{BASE}/api/analytics/attribution", timeout=20)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "provenance" in body, body
    p = body["provenance"]
    assert p["contract_version"] == 1
    assert p["source_kind"] == "derived"
    assert p["provider"] == "stoic_trades"
    assert "not broker-reconciled" in p.get("note", "").lower()


def test_safety_blocks_stats_provenance(admin_session):
    r = admin_session.get(f"{BASE}/api/safety-blocks/stats", params={"days": 7}, timeout=20)
    assert r.status_code == 200, r.text
    body = r.json()
    p = body["provenance"]
    assert p["contract_version"] == 1
    assert p["source_kind"] == "derived"
    assert p["provider"] == "stoic_safety_blocks"
    assert p["points"] == len(body.get("by_day", []))


def test_trade_replay_provenance(admin_session):
    r = admin_session.get(f"{BASE}/api/trades", params={"limit": 20}, timeout=20)
    assert r.status_code == 200, r.text
    data = r.json()
    trades = data.get("trades") if isinstance(data, dict) else data
    if not trades:
        pytest.skip("no trades for admin")
    tid = None
    for t in trades:
        tid = t.get("id") or t.get("trade_id") or t.get("_id")
        if tid:
            break
    assert tid, trades[0]
    r2 = admin_session.get(f"{BASE}/api/trades/{tid}/replay", timeout=25)
    if r2.status_code == 404:
        pytest.skip("replay unavailable for trade")
    assert r2.status_code == 200, r2.text
    p = r2.json()["provenance"]
    assert p["contract_version"] == 1
    assert p["source_kind"] == "indicative"
    assert p["provider"].startswith("replay:"), p["provider"]
    assert p.get("cache_status") in ("live", "fallback_m15")


def test_unauth_blocked():
    s = requests.Session()
    for path in ("/api/analytics/attribution", "/api/safety-blocks/stats?days=7"):
        r = s.get(f"{BASE}{path}", timeout=15)
        assert r.status_code in (401, 403), (path, r.status_code)
    # replay needs an id; use obviously fake id — should still be 401 (not 404) when unauth
    r = s.get(f"{BASE}/api/trades/nonexistent-id/replay", timeout=15)
    assert r.status_code in (401, 403), r.status_code
