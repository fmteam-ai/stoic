"""
Iter-50 full sweep — verifies all endpoints listed in review_request without opening any live trades.
Covers: auth, bot pulse, bot health page panels, auto-heal, simple mode toggle, postmortem,
analytics sessions, bridge shape, sanity routes, bot config PUT, signal generation veto.
"""
import os
import requests
import pytest

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://risk-managed-trading-4.preview.emergentagent.com").rstrip("/")
ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PASSWORD = "admin123"


@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    r = s.post(f"{BASE_URL}/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=15)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return s


# ----- AUTH -----
def test_auth_me(session):
    r = session.get(f"{BASE_URL}/api/auth/me", timeout=10)
    assert r.status_code == 200
    assert r.json().get("email") == ADMIN_EMAIL


# ----- BOT PULSE -----
def test_bot_pulse_shape(session):
    r = session.get(f"{BASE_URL}/api/bot/pulse", timeout=15)
    assert r.status_code == 200
    data = r.json()
    assert "items" in data or isinstance(data, list)


# ----- BOT HEALTH SCORE -----
def test_health_score(session):
    r = session.get(f"{BASE_URL}/api/bot/health-score", timeout=15)
    assert r.status_code == 200
    d = r.json()
    assert "score" in d
    assert 0 <= d["score"] <= 100
    assert "issues" in d


# ----- DIAGNOSTIC -----
def test_diagnostic_run(session):
    r = session.get(f"{BASE_URL}/api/diagnostic/run", timeout=20)
    assert r.status_code == 200
    d = r.json()
    assert "sections" in d or "checks" in d


# ----- AUTO-HEAL -----
def test_auto_heal_settings_get(session):
    r = session.get(f"{BASE_URL}/api/auto-heal/settings", timeout=10)
    assert r.status_code == 200

def test_auto_heal_settings_toggle(session):
    r1 = session.post(f"{BASE_URL}/api/auto-heal/settings", json={"enabled": True}, timeout=10)
    assert r1.status_code == 200
    r2 = session.get(f"{BASE_URL}/api/auto-heal/settings", timeout=10)
    assert r2.status_code == 200
    assert r2.json().get("enabled") is True
    # restore
    session.post(f"{BASE_URL}/api/auto-heal/settings", json={"enabled": False}, timeout=10)

def test_auto_heal_run_now(session):
    r = session.post(f"{BASE_URL}/api/auto-heal/run-now", json={}, timeout=30)
    assert r.status_code == 200
    # should report actions array or similar
    d = r.json()
    assert isinstance(d, dict)

def test_auto_heal_log(session):
    r = session.get(f"{BASE_URL}/api/auto-heal/log", timeout=10)
    assert r.status_code == 200
    d = r.json()
    assert "items" in d


# ----- SIMPLE MODE / PREFERENCES -----
def test_preferences_round_trip(session):
    # toggle simple_mode true
    r1 = session.post(f"{BASE_URL}/api/settings/preferences", json={"simple_mode": True}, timeout=10)
    assert r1.status_code == 200, r1.text
    r2 = session.get(f"{BASE_URL}/api/settings/preferences", timeout=10)
    assert r2.status_code == 200
    assert r2.json().get("simple_mode") is True
    # restore
    session.post(f"{BASE_URL}/api/settings/preferences", json={"simple_mode": False}, timeout=10)


# ----- POSTMORTEM -----
def test_postmortem_list(session):
    r = session.get(f"{BASE_URL}/api/postmortem", timeout=15)
    assert r.status_code == 200
    d = r.json()
    assert "items" in d or isinstance(d, list)

def test_postmortem_patterns(session):
    r = session.get(f"{BASE_URL}/api/postmortem/patterns", timeout=15)
    assert r.status_code == 200

def test_postmortem_settings(session):
    r = session.get(f"{BASE_URL}/api/postmortem/settings", timeout=10)
    assert r.status_code == 200
    d = r.json()
    assert "auto_tighten_enabled" in d

def test_postmortem_adjustments(session):
    r = session.get(f"{BASE_URL}/api/postmortem/adjustments", timeout=10)
    assert r.status_code == 200


# ----- ANALYTICS SESSIONS -----
def test_analytics_sessions(session):
    r = session.get(f"{BASE_URL}/api/analytics/sessions", timeout=15)
    assert r.status_code == 200
    d = r.json()
    # Should contain breakdown by session
    assert isinstance(d, (dict, list))


# ----- BOT CONFIG PARTIAL UPDATE (aggressive_mode + min_confidence_override) -----
def test_bot_configs_get(session):
    r = session.get(f"{BASE_URL}/api/bot/configs", timeout=10)
    assert r.status_code == 200

def test_bot_config_put_partial(session):
    # Snapshot current default config
    r = session.get(f"{BASE_URL}/api/bot/configs", timeout=10)
    items = r.json().get("items") if isinstance(r.json(), dict) else r.json()
    items = items or []
    orig_min = items[0].get("min_confidence_override") if items else None
    # Partial PATCH via PUT
    p = session.put(f"{BASE_URL}/api/bot/config", json={"min_confidence_override": 70}, timeout=10)
    assert p.status_code in (200, 204), p.text
    body = p.json() if p.status_code == 200 else {}
    assert int(body.get("min_confidence_override", 0)) == 70
    # Restore
    payload = {"min_confidence_override": 0}
    if orig_min is not None:
        payload["min_confidence_override"] = orig_min
    session.put(f"{BASE_URL}/api/bot/config", json=payload, timeout=10)


# ----- SIGNAL GENERATION (NO EXECUTE) -----
def test_signal_generate_xauusd(session):
    r = session.post(f"{BASE_URL}/api/signals/generate", json={"symbol": "XAUUSD", "risk_level": "medium"}, timeout=60)
    assert r.status_code == 200, r.text
    sig = r.json()
    assert "action" in sig
    assert "confidence" in sig
    assert "reasoning" in sig
    # iter-38: HOLD must have null entry/sl/tp
    if sig.get("action", "").upper() == "HOLD":
        assert sig.get("entry") in (None, 0, "") or sig.get("entry") is None
        assert sig.get("sl") in (None, 0, "") or sig.get("sl") is None
        assert sig.get("tp") in (None, 0, "") or sig.get("tp") is None


# ----- RESEARCH AUTO-ACCEPT ROUND TRIP (iter-44) -----
def test_research_auto_accept_round_trip(session):
    r1 = session.post(f"{BASE_URL}/api/research/auto-accept", json={"enabled": True, "min_delta_pct": 12.5}, timeout=10)
    assert r1.status_code == 200, r1.text
    r2 = session.get(f"{BASE_URL}/api/research/auto-accept", timeout=10)
    assert r2.status_code == 200
    d = r2.json()
    assert d.get("enabled") is True
    assert float(d.get("min_delta_pct", 0)) == pytest.approx(12.5, rel=0.01)
    session.post(f"{BASE_URL}/api/research/auto-accept", json={"enabled": False, "min_delta_pct": 10.0}, timeout=10)


# ----- SANITY: routes that should be 200 -----
SANITY_GET = [
    "/api/portfolio/snapshot",
    "/api/safety-blocks/list",
    "/api/safety-blocks/stats",
    "/api/strategies",
    "/api/bot/presets",
    "/api/research/proposals",
    "/api/macro/snapshot",
    "/api/macro/freeze/XAUUSD",
    "/api/macro/freeze/BTCUSD",
    "/api/calendar?days=1",
    "/api/sentiment/XAUUSD",
    "/api/data-freshness",
    "/api/notifications/prefs",
    "/api/telegram/webhook/status",
    "/api/affiliate/status",
    "/api/subscription/status",
    "/api/shadow/performance",
    "/api/agents/activity?limit=3",
    "/api/analytics/sessions",
    "/api/analytics/attribution",
    "/api/execution/quality?symbol=XAUUSD",
    "/api/crypto/status",
    "/api/crypto/accounts",
    "/api/bot/configs",
    "/api/bot/quick-actions",
    "/api/auto-heal/settings",
    "/api/auto-heal/log",
    "/api/postmortem",
    "/api/postmortem/patterns",
    "/api/postmortem/adjustments",
    "/api/postmortem/settings",
    "/api/bot/pulse",
    "/api/bot/health-score",
    "/api/diagnostic/run",
]

@pytest.mark.parametrize("path", SANITY_GET)
def test_sanity_get(session, path):
    r = session.get(f"{BASE_URL}{path}", timeout=20)
    assert r.status_code == 200, f"{path} -> {r.status_code} {r.text[:300]}"


# ----- BRIDGE: requires API key (verify shape — should 401/403 without key, not 500) -----
def test_bridge_heartbeat_requires_key():
    r = requests.post(f"{BASE_URL}/api/bridge/heartbeat", json={}, timeout=20)
    assert r.status_code in (401, 403, 422), f"got {r.status_code}"

def test_bridge_poll_trades_requires_key():
    r = requests.post(f"{BASE_URL}/api/bridge/poll-trades", json={}, timeout=20)
    assert r.status_code in (401, 403, 422), f"got {r.status_code}"

def test_bridge_external_deal_requires_key():
    r = requests.post(f"{BASE_URL}/api/bridge/external-deal", json={}, timeout=20)
    assert r.status_code in (401, 403, 422), f"got {r.status_code}"
