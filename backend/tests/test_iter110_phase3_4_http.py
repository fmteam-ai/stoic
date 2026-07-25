"""iter-110 live HTTP checks — Phase 3/4 live-ops endpoints via preview URL.

SAFETY: never fires a real operator action against the admin account —
only GET endpoints plus the invalid-action 400 path are exercised.
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

ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PASSWORD = "admin123"


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
               timeout=30)
    assert r.status_code == 200, f"login failed {r.status_code} {r.text}"
    return s


def _csrf_headers(session):
    csrf = session.cookies.get("csrf_token")
    return {"X-CSRF-Token": csrf} if csrf else {}


def test_capital_stage_shape(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/risk/capital-stage", timeout=30)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["stage"] in (1, 2, 3)
    assert d["label"] in ("PILOT", "SCALE", "DEPLOY")
    assert "next_milestone" in d
    ev = d["evidence"]
    for k in ("n", "mean_r", "ci95_lower_r", "max_drawdown_r"):
        assert k in ev, f"missing evidence key {k}"
    if d["stage"] == 3:
        assert d["risk_cap_pct"] is None
    else:
        assert d["risk_cap_pct"] in (0.25, 0.5)


def test_subsystem_health_shape(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/subsystems/health", timeout=30)
    assert r.status_code == 200, r.text
    d = r.json()
    expected = {"learning_engine", "execution_engine", "risk_engine",
                "broker_engine", "market_engine"}
    assert set(d["subsystems"].keys()) == expected
    assert d["multiplier"] in (1.0, 0.75, 0.5)
    assert "reason" in d


def test_operator_actions_catalog(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/operator/actions", timeout=30)
    assert r.status_code == 200, r.text
    actions = {a["action"] for a in r.json()["actions"]}
    assert actions == {"freeze_trading", "reduce_exposure", "pause_symbol",
                       "defensive_mode", "panic_mode"}


def test_operator_action_rejects_unknown(admin_session):
    r = admin_session.post(
        f"{BASE_URL}/api/operator/action",
        json={"action": "raise_all_risk"},
        headers=_csrf_headers(admin_session), timeout=30)
    assert r.status_code == 400, r.text


def test_risk_realtime_composite(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/risk/realtime", timeout=45)
    assert r.status_code == 200, r.text
    d = r.json()
    for k in ("accounts", "open_trades", "broker_intel",
              "shadow_health", "subsystems", "capital_stage"):
        assert k in d, f"missing key {k}"
    assert "total" in d["open_trades"]
    assert "by_symbol" in d["open_trades"]
    assert d["capital_stage"]["stage"] in (1, 2, 3)


def test_liveops_requires_auth():
    for path in ("/api/risk/capital-stage", "/api/subsystems/health",
                 "/api/operator/actions", "/api/risk/realtime"):
        r = requests.get(f"{BASE_URL}{path}", timeout=30)
        assert r.status_code in (401, 403), f"{path} → {r.status_code}"
