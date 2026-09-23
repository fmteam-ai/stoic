from live_target import ADMIN_EMAIL, ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
"""iter-55 · Daily Auto-Learning — evidence-gated auto-apply / auto-revert
(loss_advisor.py) + settings/guards HTTP API."""
import os
import sys

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loss_advisor import (  # noqa: E402
    _qualifies, live_guard_block, AUTO_APPLY_MIN_NET, AUTO_APPLY_RATIO,
)

from live_target import require_live_base_url
BASE_URL = require_live_base_url()
pass  # ADMIN_EMAIL comes from live_target
pass  # ADMIN_PASSWORD comes from live_target
# ----------------------------------------------------------- evidence bar
class TestQualifies:
    def _m(self, net, saved, missed, testable=True):
        return {"type": "symbol_pause", "evidence": {
            "testable": testable, "net_effect": net,
            "losses_avoided": saved, "wins_missed": missed}}

    def test_passes_strict_bar(self):
        assert _qualifies(self._m(150, 200, 50)) is True

    def test_fails_below_min_net(self):
        assert _qualifies(self._m(AUTO_APPLY_MIN_NET - 1, 200, 101)) is False

    def test_fails_ratio(self):
        # net ok but saved < 2x missed
        assert _qualifies(self._m(120, 220, 100 * AUTO_APPLY_RATIO / 2 + 20)) is False

    def test_fails_untestable(self):
        assert _qualifies(self._m(500, 500, 0, testable=False)) is False

    def test_fails_no_evidence(self):
        assert _qualifies({"type": "other"}) is False

    def test_boundary_exact(self):
        assert _qualifies(self._m(AUTO_APPLY_MIN_NET, AUTO_APPLY_RATIO * 50, 50)) is True


# ------------------------------------------------------- live guard block
def guard(mtype, params, net=150):
    return {"measure": {"type": mtype, "title": f"t-{mtype}", "params": params},
            "evidence": {"testable": True, "net_effect": net}, "active": True}


class TestLiveGuardBlock:
    def test_symbol_pause_blocks_matching_signal(self):
        g = [guard("symbol_pause", {"symbol": "US30"})]
        sig = {"symbol": "US30.cash", "action": "BUY", "confidence": 90}
        assert live_guard_block(sig, g) is not None

    def test_symbol_pause_ignores_other_symbol(self):
        g = [guard("symbol_pause", {"symbol": "US30"})]
        sig = {"symbol": "XAUUSD", "action": "SELL", "confidence": 90}
        assert live_guard_block(sig, g) is None

    def test_min_confidence_blocks_low_conf(self):
        g = [guard("min_confidence", {"value": 80})]
        assert live_guard_block({"symbol": "XAUUSD", "action": "BUY", "confidence": 70}, g) is not None
        assert live_guard_block({"symbol": "XAUUSD", "action": "BUY", "confidence": 85}, g) is None

    def test_session_block(self):
        g = [guard("session_block", {"session": "asia"})]
        sig = {"symbol": "XAUUSD", "action": "SELL", "session": {"primary": "ASIA"}}
        assert live_guard_block(sig, g) is not None
        sig2 = {"symbol": "XAUUSD", "action": "SELL", "session": {"primary": "london"}}
        assert live_guard_block(sig2, g) is None

    def test_block_reason_mentions_evidence(self):
        g = [guard("symbol_pause", {"symbol": "US30"}, net=222)]
        reason = live_guard_block({"symbol": "US30", "action": "BUY"}, g)
        assert "Auto-guard" in reason and "+222" in reason

    def test_latest_evidence_preferred(self):
        g = guard("symbol_pause", {"symbol": "US30"}, net=100)
        g["latest_evidence"] = {"testable": True, "net_effect": 333}
        reason = live_guard_block({"symbol": "US30", "action": "BUY"}, [g])
        assert "+333" in reason

    def test_untestable_guard_never_blocks(self):
        g = [guard("other", {})]
        assert live_guard_block({"symbol": "US30", "action": "BUY"}, g) is None


# ------------------------------------------------------------------- HTTP
@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=15)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return s


class TestSettingsAPI:
    def test_get_settings_has_auto_apply(self, session):
        r = session.get(f"{BASE_URL}/api/postmortem/settings", timeout=10)
        assert r.status_code == 200
        assert "auto_apply_guards" in r.json()

    def test_toggle_roundtrip(self, session):
        orig = session.get(f"{BASE_URL}/api/postmortem/settings", timeout=10).json()
        r = session.post(f"{BASE_URL}/api/postmortem/settings",
                         json={"auto_apply_guards": False}, timeout=10)
        assert r.status_code == 200
        assert r.json()["auto_apply_guards"] is False
        # auto_tighten preserved
        assert r.json()["auto_tighten_enabled"] == orig["auto_tighten_enabled"]
        got = session.get(f"{BASE_URL}/api/postmortem/settings", timeout=10).json()
        assert got["auto_apply_guards"] is False
        # restore
        r2 = session.post(f"{BASE_URL}/api/postmortem/settings",
                          json={"auto_apply_guards": orig["auto_apply_guards"]}, timeout=10)
        assert r2.status_code == 200


class TestGuardsAPI:
    def test_list_guards(self, session):
        r = session.get(f"{BASE_URL}/api/postmortem/guards", timeout=10)
        assert r.status_code == 200
        assert isinstance(r.json().get("guards"), list)

    def test_revert_invalid_id(self, session):
        r = session.post(f"{BASE_URL}/api/postmortem/guards/not-an-id/revert", timeout=10)
        assert r.status_code == 400

    def test_revert_unknown_id(self, session):
        r = session.post(f"{BASE_URL}/api/postmortem/guards/665f00000000000000000000/revert", timeout=10)
        assert r.status_code == 404

    def test_reviews_list(self, session):
        r = session.get(f"{BASE_URL}/api/postmortem/reviews", timeout=10)
        assert r.status_code == 200
        assert isinstance(r.json().get("reviews"), list)


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
