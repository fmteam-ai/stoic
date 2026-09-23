from live_target import ADMIN_EMAIL, ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
"""iter-96 (reviews iter-111): HTTP verification of the six safety
corrections against the live preview backend.

Only READ endpoints are tested against admin. The one mutating call
(bot config update) touches a benign field (loss_cooldown_minutes) and
DOES NOT alter operational_mode (per E1 safety note)."""
import os
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
pass  # ADMIN_EMAIL comes from live_target
ADMIN_PASS = ADMIN_PASSWORD


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASS},
               timeout=15)
    assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text[:200]}"
    return s


# ── #1 shadow health: fail-closed fields ────────────────────────
class TestShadowHealth:
    def test_shape_and_fail_closed_fields(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/shadow/health", timeout=10)
        assert r.status_code == 200, r.text[:200]
        data = r.json()
        # Top-level new fields
        for k in ("fail_closed", "missing_components", "stale_components",
                  "components", "details", "promotions_paused", "overall"):
            assert k in data, f"missing top-level {k}"
        assert isinstance(data["fail_closed"], bool)
        assert isinstance(data["missing_components"], list)
        assert isinstance(data["stale_components"], list)

        # exactly 7 known component keys, none null (fail-closed scoring)
        expected = {"data_freshness", "regime_confidence",
                    "calibration_quality", "execution_quality",
                    "broker_stability", "worker_health", "synchronization"}
        assert set(data["components"].keys()) == expected
        for k, v in data["components"].items():
            assert v is not None, f"component {k} is null (should be 0)"
            assert isinstance(v, (int, float))

        # details buckets
        assert "data_freshness_feeds" in data["details"]
        assert "broker_stability_accounts" in data["details"]
        assert isinstance(data["details"]["data_freshness_feeds"], dict)
        assert isinstance(data["details"]["broker_stability_accounts"], list)

        # promotions_paused rule
        expected_paused = (data["overall"] < 60) or data["fail_closed"]
        assert data["promotions_paused"] == expected_paused

    def test_per_feed_shape(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/shadow/health", timeout=10)
        feeds = r.json()["details"]["data_freshness_feeds"]
        for sym, info in feeds.items():
            assert "age_secs" in info or "score" in info, f"feed {sym} missing fields: {info}"
            assert "score" in info

    def test_per_account_shape(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/shadow/health", timeout=10)
        accts = r.json()["details"]["broker_stability_accounts"]
        for a in accts:
            assert "label" in a
            assert "live" in a
            assert "score" in a


# ── #2 mode guardian ────────────────────────────────────────────
class TestModeGuardian:
    def test_guardian_shape(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/modes/guardian", timeout=10)
        assert r.status_code == 200, r.text[:200]
        data = r.json()
        assert "recovery" in data
        assert set(["applicable", "eligible", "reason"]).issubset(
            data["recovery"].keys())
        assert "recent_demotions" in data
        assert isinstance(data["recent_demotions"], list)
        assert data.get("green_hours_required") == 24


# ── #3 broker qualification: spec checks ────────────────────────
class TestBrokerQualification:
    def test_qualification_matrix_has_spec_checks(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/broker-intel/qualification",
                              timeout=15)
        assert r.status_code == 200, r.text[:200]
        data = r.json()
        # Response is either {accounts:[...]} or a list
        accts = data.get("accounts", data) if isinstance(data, dict) else data
        if not accts:
            pytest.skip("no accounts to qualify in preview")
        for acc in accts:
            checks = acc.get("checks") or {}
            for k in ("stop_restrictions", "freeze_levels",
                      "symbol_specs", "dst_handling"):
                assert k in checks, f"account {acc.get('label')} missing {k}: keys={list(checks.keys())}"
                assert checks[k]["status"] in (
                    "observed", "not_reported", "pass", "fail"), \
                    f"unexpected status {checks[k]}"
            # If CERTIFIED, all four must be observed
            if acc.get("tier") == "CERTIFIED":
                for k in ("stop_restrictions", "freeze_levels",
                          "symbol_specs", "dst_handling"):
                    assert checks[k]["status"] == "observed", \
                        f"CERTIFIED without {k} observed"

    def test_qualification_note_mentions_v154(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/broker-intel/qualification",
                              timeout=15)
        text = r.text
        # Global note or per-account detail should mention v1.54
        assert "1.54" in text or "v1.54" in text, "no v1.54 mention in qualification response"


# ── #4 atomic bot config update ─────────────────────────────────
class TestBotConfigAtomicUpdate:
    def test_update_loss_cooldown_persists(self, admin_session):
        # GET current
        r_get = admin_session.get(f"{BASE_URL}/api/bot/config", timeout=10)
        assert r_get.status_code == 200
        cfg_before = r_get.json()
        original = cfg_before.get("loss_cooldown_minutes", 15)
        new_val = 25 if original != 25 else 20

        # PUT update — DO NOT touch operational_mode
        r_put = admin_session.put(
            f"{BASE_URL}/api/bot/config",
            json={"loss_cooldown_minutes": new_val},
            timeout=15)
        # Some backends use POST — try POST if PUT fails with 405
        if r_put.status_code == 405:
            r_put = admin_session.post(
                f"{BASE_URL}/api/bot/config",
                json={"loss_cooldown_minutes": new_val},
                timeout=15)
        assert r_put.status_code in (200, 201), \
            f"update failed: {r_put.status_code} {r_put.text[:200]}"

        # GET again — should reflect
        r_get2 = admin_session.get(f"{BASE_URL}/api/bot/config", timeout=10)
        assert r_get2.status_code == 200
        cfg_after = r_get2.json()
        assert cfg_after.get("loss_cooldown_minutes") == new_val, \
            f"expected {new_val}, got {cfg_after.get('loss_cooldown_minutes')}"

        # Restore original
        admin_session.put(
            f"{BASE_URL}/api/bot/config",
            json={"loss_cooldown_minutes": original},
            timeout=15)


# ── #5 EA version = latest ───────────────────────────────────────
class TestEAVersion:
    def test_ea_version_154(self, admin_session):
        from ea_version import current_ea_version
        v = current_ea_version()
        # Try known setup/diagnostic endpoints
        candidates = [
            "/api/bot/health-score",
            "/api/setup/ea-version",
            "/api/diagnostic/ea-version",
        ]
        found = False
        for path in candidates:
            r = admin_session.get(f"{BASE_URL}{path}", timeout=10)
            if r.status_code == 200 and v in r.text:
                found = True
                break
        if not found:
            # fallback: look at bot health / any endpoint reporting ea_latest_version
            r = admin_session.get(f"{BASE_URL}/api/bot/health", timeout=10)
            if r.status_code == 200 and v in r.text:
                found = True
        assert found, f"no endpoint reports EA v{v}"


# ── #6 liveops regression ───────────────────────────────────────
class TestLiveOpsRegression:
    @pytest.mark.parametrize("path", [
        "/api/risk/capital-stage",
        "/api/subsystems/health",
        "/api/risk/realtime",
        "/api/operator/actions",
    ])
    def test_endpoints_200(self, admin_session, path):
        r = admin_session.get(f"{BASE_URL}{path}", timeout=15)
        assert r.status_code == 200, f"{path} → {r.status_code} {r.text[:200]}"
        # Must return valid JSON
        data = r.json()
        assert data is not None


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
