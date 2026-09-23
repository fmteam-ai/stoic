from live_target import ADMIN_EMAIL, ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
"""iter-117 backlog: SilentBotBanner (bot/pulse+notable), AgentReportCard, PartnerBroker.

Covers:
  - GET /api/bot/pulse now includes `notable` + `notable_stale_seconds`
  - GET /api/agents/report-card shape, verdict enum, cache (2nd call w/o force = same built_at)
  - GET /api/partners/brokers lazy-seeds 3 defaults, sorted, is_admin flag
  - POST /api/partners/brokers admin upsert (placeholder flip), non-admin 403
  - DELETE /api/partners/brokers admin only, non-admin 403
  - POST /api/partners/brokers/{id}/click increments clicks + returns url
"""
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import os
import time
import uuid
import requests
import pytest

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
API = f"{BASE_URL}/api"


# ---------- fixtures ----------
@pytest.fixture(scope="module")
def admin_sess():
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
               timeout=15)
    assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text}"
    return s


@pytest.fixture(scope="module")
def user_sess():
    """Register a fresh non-admin user with email_verified=true (via mongo bypass)."""
    from pymongo import MongoClient
    email = f"iter117user_{uuid.uuid4().hex[:8]}@test.com"
    pw = "Uq8#Rn4jS6wLbM3z"
    r = requests.post(f"{API}/auth/register",
                      json={"email": email, "password": pw, "name": "iter117",
                            "terms_agreed": True}, timeout=15)
    assert r.status_code in (200, 201), f"register: {r.status_code} {r.text}"
    # Flip verified (load MONGO/DB from backend .env)
    from pathlib import Path
    envfile = Path(_os.path.join(_BACKEND_DIR, ".env")).read_text()
    envs = {}
    for line in envfile.splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            envs[k.strip()] = v.strip().strip('"').strip("'")
    mongo = MongoClient(envs.get("MONGO_URL", "mongodb://localhost:27017"))
    dbname = envs.get("DB_NAME", "ai_trading_bot")
    mongo[dbname].users.update_one({"email": email},
                                   {"$set": {"email_verified": True}})
    s = requests.Session()
    r = s.post(f"{API}/auth/login", json={"email": email, "password": pw}, timeout=15)
    assert r.status_code == 200, f"user login: {r.status_code} {r.text}"
    yield s
    # cleanup
    try:
        mongo[dbname].users.delete_one({"email": email})
    except Exception:
        pass


# ---------- bot pulse notable ----------
class TestBotPulseNotable:
    def test_pulse_shape(self, admin_sess):
        r = admin_sess.get(f"{API}/bot/pulse", timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert "items" in data
        assert isinstance(data["items"], list)
        for it in data["items"]:
            assert "pulse" in it
            assert "stale_seconds" in it
            # New fields per iter-117
            assert "notable" in it, f"missing notable in {it}"
            assert "notable_stale_seconds" in it, f"missing notable_stale_seconds in {it}"

    def test_notable_not_routine_cooldown(self, admin_sess):
        r = admin_sess.get(f"{API}/bot/pulse", timeout=15)
        data = r.json()
        for it in data["items"]:
            n = it.get("notable")
            if not n:
                continue
            reason = (n.get("reason") or "").lower()
            assert "cooldown active" not in reason or n.get("action") != "SKIP", \
                f"routine cooldown surfaced as notable: {n}"
            # Required subfields
            assert "ts" in n
            assert "action" in n
            assert "reason" in n
            assert "level" in n


# ---------- agents/report-card ----------
class TestAgentReportCard:
    def test_shape(self, admin_sess):
        r = admin_sess.get(f"{API}/agents/report-card", timeout=30)
        assert r.status_code == 200, r.text
        data = r.json()
        # baseline
        assert "baseline" in data
        b = data["baseline"]
        for k in ("closed_trades", "win_rate", "avg_win", "avg_loss",
                  "ev_per_trade", "window_days"):
            assert k in b, f"baseline missing {k}"
        # agents
        assert "agents" in data
        assert isinstance(data["agents"], list)
        assert len(data["agents"]) >= 10, "expected 10+ agents in report"
        valid_verdicts = {"QUIET", "KEEP ENFORCE", "CONSIDER ADVISE", "MONITORING"}
        for a in data["agents"]:
            for k in ("name", "slug", "hard_blocks", "soft_flags",
                      "est_pnl_impact", "verdict"):
                assert k in a, f"agent missing {k}: {a}"
            assert a["verdict"] in valid_verdicts, f"bad verdict: {a['verdict']}"
        # totals + method
        assert "totals" in data
        assert "hard_blocks" in data["totals"]
        assert "est_pnl_impact" in data["totals"]
        assert isinstance(data.get("method"), str) and len(data["method"]) > 0
        assert "built_at" in data

    def test_cache_second_call_same_built_at(self, admin_sess):
        # Force rebuild
        r1 = admin_sess.get(f"{API}/agents/report-card?force=true", timeout=30)
        assert r1.status_code == 200
        built1 = r1.json()["built_at"]
        # Second call without force should return cache with same built_at
        time.sleep(0.5)
        r2 = admin_sess.get(f"{API}/agents/report-card", timeout=30)
        assert r2.status_code == 200
        built2 = r2.json()["built_at"]
        assert built1 == built2, f"cache miss: {built1} != {built2}"

    def test_force_rebuilds(self, admin_sess):
        r1 = admin_sess.get(f"{API}/agents/report-card?force=true", timeout=30)
        built1 = r1.json()["built_at"]
        time.sleep(1.1)  # ensure timestamp differs
        r2 = admin_sess.get(f"{API}/agents/report-card?force=true", timeout=30)
        built2 = r2.json()["built_at"]
        assert built1 != built2, "force=true should rebuild (different built_at)"


# ---------- partners/brokers ----------
class TestPartnerBrokers:
    def test_list_lazy_seed_defaults(self, admin_sess):
        r = admin_sess.get(f"{API}/partners/brokers", timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert "items" in data
        assert "is_admin" in data
        assert data["is_admin"] is True
        names = [b["name"] for b in data["items"]]
        # At least the 3 defaults present (some tests may have added more)
        for expected in ("Exness", "IC Markets", "Vantage"):
            assert expected in names, f"default {expected} missing"
        # Check placeholder flag on defaults
        for b in data["items"]:
            if b["name"] in ("Exness", "IC Markets", "Vantage") and "PLACEHOLDER" in (b.get("ib_link") or ""):
                assert b["is_placeholder"] is True
        # Sorted by 'sort' ascending
        sorts = [b.get("sort", 99) for b in data["items"]]
        assert sorts == sorted(sorts), f"not sorted: {sorts}"

    def test_non_admin_list_ok_but_is_admin_false(self, user_sess):
        r = user_sess.get(f"{API}/partners/brokers", timeout=15)
        assert r.status_code == 200
        assert r.json()["is_admin"] is False

    def test_non_admin_post_403(self, user_sess):
        payload = {"name": "Rogue", "ib_link": "https://example.com/PLACEHOLDER"}
        r = user_sess.post(f"{API}/partners/brokers", json=payload, timeout=15)
        assert r.status_code == 403, f"expected 403, got {r.status_code} {r.text}"

    def test_non_admin_delete_403(self, user_sess, admin_sess):
        # Get an existing broker id
        r = admin_sess.get(f"{API}/partners/brokers", timeout=15)
        bid = r.json()["items"][0]["id"]
        r2 = user_sess.delete(f"{API}/partners/brokers/{bid}", timeout=15)
        assert r2.status_code == 403

    def test_admin_upsert_placeholder_flip(self, admin_sess):
        # Get Exness broker
        r = admin_sess.get(f"{API}/partners/brokers", timeout=15)
        items = r.json()["items"]
        exness = next(b for b in items if b["name"] == "Exness")
        original_link = exness["ib_link"]

        # Update to a REAL link (no PLACEHOLDER) → should flip is_placeholder=false
        real_link = "https://one.exness-track.com/a/REAL_TRACKING_ID_TEST"
        payload = {
            "id": exness["id"],
            "name": exness["name"],
            "tagline": exness["tagline"],
            "regulation": exness["regulation"],
            "min_deposit": exness["min_deposit"],
            "ib_link": real_link,
            "highlight": exness["highlight"],
            "sort": exness["sort"],
            "active": True,
        }
        r2 = admin_sess.post(f"{API}/partners/brokers", json=payload, timeout=15)
        assert r2.status_code == 200, r2.text
        saved = r2.json()
        assert saved["ib_link"] == real_link
        assert saved["is_placeholder"] is False, "should flip to False for non-placeholder link"

        # Verify via GET
        r3 = admin_sess.get(f"{API}/partners/brokers", timeout=15)
        fetched = next(b for b in r3.json()["items"] if b["id"] == exness["id"])
        assert fetched["ib_link"] == real_link
        assert fetched["is_placeholder"] is False

        # Revert
        payload["ib_link"] = original_link
        admin_sess.post(f"{API}/partners/brokers", json=payload, timeout=15)

    def test_click_increments_and_returns_url(self, admin_sess):
        r = admin_sess.get(f"{API}/partners/brokers", timeout=15)
        broker = r.json()["items"][0]
        bid = broker["id"]
        before = broker["clicks"]

        rc = admin_sess.post(f"{API}/partners/brokers/{bid}/click", timeout=15)
        assert rc.status_code == 200
        assert "url" in rc.json()
        assert rc.json()["url"] == broker["ib_link"]

        r2 = admin_sess.get(f"{API}/partners/brokers", timeout=15)
        after_broker = next(b for b in r2.json()["items"] if b["id"] == bid)
        assert after_broker["clicks"] == before + 1

    def test_admin_delete_and_readd(self, admin_sess):
        # Create a throwaway broker to safely test delete
        payload = {
            "name": "TEST_DELETE_ME",
            "tagline": "throwaway",
            "regulation": "-",
            "min_deposit": "-",
            "ib_link": "https://example.com/PLACEHOLDER",
            "highlight": False,
            "sort": 999,
            "active": True,
        }
        r = admin_sess.post(f"{API}/partners/brokers", json=payload, timeout=15)
        assert r.status_code == 200
        bid = r.json()["id"]

        rd = admin_sess.delete(f"{API}/partners/brokers/{bid}", timeout=15)
        assert rd.status_code == 200

        # Deleting again → 404
        rd2 = admin_sess.delete(f"{API}/partners/brokers/{bid}", timeout=15)
        assert rd2.status_code == 404


# ---------- regression sanity ----------
class TestRegression:
    def test_market_posture(self, admin_sess):
        r = admin_sess.get(f"{API}/posture/summary", timeout=15)
        assert r.status_code in (200, 404)  # endpoint variance tolerated

    def test_bot_pulse_existing_consumers(self, admin_sess):
        r = admin_sess.get(f"{API}/bot/pulse", timeout=15)
        assert r.status_code == 200
        data = r.json()
        # loop_interval_sec still present
        assert "loop_interval_sec" in data

    def test_accounts_list(self, admin_sess):
        r = admin_sess.get(f"{API}/accounts/", timeout=15)
        assert r.status_code in (200, 307)
        if r.status_code == 307:
            r = admin_sess.get(f"{API}/accounts", timeout=15)
        assert r.status_code == 200


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
