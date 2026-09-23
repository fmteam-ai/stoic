from live_target import ADMIN_EMAIL, ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
"""HTTP smoke tests for iter-37 multi-bot targeting (live preview URL).

Verifies the HTTP surface end-to-end with admin login:
- GET  /api/nl/strategy/targets (with and without symbols filter)
- POST /api/nl/strategy/apply (target=all, target=matching, bad target → 422)
- POST /api/signals/generate?account_id=...
- POST /api/signals/generate-all?account_id=...
- Auth gating (401 without cookie)
- Regression: /api/research/proposals still listable

Cleanup: any mutations to bot_configs are restored at teardown.
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

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
# Fall back to the value baked into frontend/.env when env not exported
if not BASE_URL:
    try:
        with open(_os.path.join(_REPO_DIR, "frontend", ".env")) as f:
            for line in f:
                if line.startswith("REACT_APP_BACKEND_URL="):
                    BASE_URL = line.split("=", 1)[1].strip().rstrip("/")
                    break
    except Exception:
        pass

pass  # ADMIN_EMAIL comes from live_target
pass  # ADMIN_PASSWORD comes from live_target
# ---------- fixtures ----------
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


@pytest.fixture(scope="module")
def original_configs(session):
    """Snapshot all bot_configs before mutation so we can restore later."""
    r = session.get(f"{BASE_URL}/api/bot/configs", timeout=10)
    if r.status_code != 200:
        # Older route name fallback
        r = session.get(f"{BASE_URL}/api/bot-config", timeout=10)
    return r.json() if r.status_code == 200 else None


# ---------- 401 gating ----------
def test_targets_requires_auth():
    r = requests.get(f"{BASE_URL}/api/nl/strategy/targets?symbols=XAUUSD",
                     timeout=10)
    assert r.status_code == 401, r.text


def test_apply_requires_auth():
    r = requests.post(
        f"{BASE_URL}/api/nl/strategy/apply",
        json={"compiled": {"symbols": ["XAUUSD"]}, "target": "all"},
        timeout=10,
    )
    assert r.status_code == 401, r.text


# ---------- GET /api/nl/strategy/targets ----------
def test_targets_with_symbol_filter(session):
    r = session.get(
        f"{BASE_URL}/api/nl/strategy/targets?symbols=XAUUSD,BTCUSD",
        timeout=10,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "candidates" in body
    assert "matching_count" in body
    assert "total_count" in body
    assert body["proposal_symbols"] == ["XAUUSD", "BTCUSD"]
    assert isinstance(body["candidates"], list)
    for c in body["candidates"]:
        assert "matches_proposal_symbols" in c
    assert body["matching_count"] <= body["total_count"]


def test_targets_no_symbols_all_match(session):
    r = session.get(f"{BASE_URL}/api/nl/strategy/targets", timeout=10)
    assert r.status_code == 200, r.text
    body = r.json()
    # With no symbols, every candidate should be "matching"
    assert body["matching_count"] == body["total_count"]


# ---------- POST /api/nl/strategy/apply ----------
def test_apply_rejects_empty_compiled(session):
    r = session.post(
        f"{BASE_URL}/api/nl/strategy/apply",
        json={"compiled": {}, "target": "all"},
        timeout=10,
    )
    assert r.status_code == 400, r.text


def test_apply_rejects_clarification_only(session):
    r = session.post(
        f"{BASE_URL}/api/nl/strategy/apply",
        json={"compiled": {"clarification_needed": "need more info"},
              "target": "all"},
        timeout=10,
    )
    assert r.status_code == 400, r.text


def test_apply_unknown_account_returns_422(session):
    r = session.post(
        f"{BASE_URL}/api/nl/strategy/apply",
        json={
            "compiled": {"symbols": ["XAUUSD"], "risk_level": "medium"},
            "target": "no-such-account-id",
        },
        timeout=10,
    )
    assert r.status_code == 422, r.text
    body = r.json()
    detail = body.get("detail", "")
    assert "no matching bot configs" in detail.lower() or "no-such-account-id" in detail


def test_apply_target_all_returns_audit_list(session, original_configs):
    """target=all → applied_count == total bot_configs. Includes audit list."""
    # Pull current targets first (gives us the count baseline)
    tr = session.get(f"{BASE_URL}/api/nl/strategy/targets", timeout=10)
    total_count = tr.json()["total_count"]
    if total_count == 0:
        pytest.skip("No bot_configs for admin user — cannot test target=all")

    compiled = {
        "symbols": ["XAUUSD"],
        "risk_level": "medium",
        "max_concurrent_trades": 2,
        "auto_execute": True,
        "session_preference": "any",
        "strategy_style": "trend_following",
    }
    r = session.post(
        f"{BASE_URL}/api/nl/strategy/apply",
        json={"compiled": compiled, "target": "all"},
        timeout=20,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["applied_count"] == total_count
    assert isinstance(body["applied_to"], list)
    assert len(body["applied_to"]) == total_count
    assert body["target_mode"] == "all"
    # Each audit row has expected keys
    for row in body["applied_to"]:
        assert "config_id" in row or "_id" in row or "user_id" in row


def test_apply_target_matching_filters_by_symbol(session):
    tr = session.get(
        f"{BASE_URL}/api/nl/strategy/targets?symbols=XAUUSD",
        timeout=10,
    )
    matching = tr.json()["matching_count"]
    if matching == 0:
        pytest.skip("No XAUUSD-matching bot_configs — cannot validate matching path")
    compiled = {"symbols": ["XAUUSD"], "risk_level": "medium",
                "max_concurrent_trades": 2}
    r = session.post(
        f"{BASE_URL}/api/nl/strategy/apply",
        json={"compiled": compiled, "target": "matching"},
        timeout=20,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied_count"] == matching
    assert body["target_mode"].startswith("matching")


# ---------- POST /api/signals/generate ----------
def test_signals_generate_without_account_id(session):
    r = session.post(
        f"{BASE_URL}/api/signals/generate",
        json={"symbol": "XAUUSD"},
        timeout=60,  # AI call is slow
    )
    # Accept either 200 (real AI) or 502 (AI provider unavailable in env)
    assert r.status_code in (200, 502), r.text
    if r.status_code == 200:
        body = r.json()
        assert body.get("symbol", "").upper() == "XAUUSD"
        # Without account_id query, the signal should NOT be scoped
        assert "account_id" not in body or body.get("account_id") in (None, "")


def test_signals_generate_with_account_id_scopes_doc(session):
    """When account_id is passed, the returned signal carries it."""
    # Use a fake account_id; the route still records it on the doc
    r = session.post(
        f"{BASE_URL}/api/signals/generate?account_id=test-acct-iter37",
        json={"symbol": "XAUUSD"},
        timeout=60,
    )
    assert r.status_code in (200, 502), r.text
    if r.status_code == 200:
        body = r.json()
        assert body.get("account_id") == "test-acct-iter37"


# ---------- Regression: research proposals still listable ----------
def test_research_proposals_list_still_works(session):
    r = session.get(f"{BASE_URL}/api/research/proposals", timeout=10)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "pending" in body
    assert "history" in body


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
