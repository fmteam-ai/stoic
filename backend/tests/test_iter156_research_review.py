from live_target import ADMIN_EMAIL, ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
"""iter-156 · Phase B Analytics & Scalp Review research views backend tests.

Covers:
  (1) GET /api/analytics/research (days=90, days=7, days=9999 clamp)
  (2) GET /api/scalp/review
  (3) GET /api/scalp/executions rows include 'reconciliation' if any
  (4) GET /api/trades/{id}/audit execution_summary includes 'broker_error'
"""
import os as _os
import requests
import pytest

_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)

from dotenv import load_dotenv  # noqa: E402
from live_target import require_live_base_url
load_dotenv(_os.path.join(_BACKEND_DIR, ".env"))

BASE = require_live_base_url()


def _login():
    s = requests.Session()
    r = s.post(f"{BASE}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
               timeout=15)
    assert r.status_code == 200, r.text
    csrf = s.cookies.get("csrf_token")
    if csrf:
        s.headers.update({"X-CSRF-Token": csrf})
    return s


@pytest.fixture(scope="module")
def sess():
    return _login()


# ---------------- BACKEND 1: /api/analytics/research
class TestAnalyticsResearch:
    def test_research_days_90(self, sess):
        r = sess.get(f"{BASE}/api/analytics/research?days=90", timeout=30)
        assert r.status_code == 200, r.text
        j = r.json()

        # Top-level keys
        for k in ("trades", "calibration", "symbols", "walk_forward",
                  "walk_forward_stability", "regimes", "decay", "execution"):
            assert k in j, f"missing key {k}"

        # trades count
        assert isinstance(j["trades"], int)
        assert j["trades"] > 0, f"expected non-zero trades, got {j['trades']}"
        # spec says ~488
        assert 300 <= j["trades"] <= 700, f"trades={j['trades']} out of expected band"

        # calibration
        assert isinstance(j["calibration"], list)
        assert len(j["calibration"]) >= 1
        for row in j["calibration"]:
            assert "bucket" in row and "n" in row
            # win_rate/ci_low/ci_high/gap may be None when n=0
            for k in ("win_rate", "ci_low", "ci_high", "gap"):
                assert k in row, f"calibration row missing {k}"

        # symbols
        assert isinstance(j["symbols"], list)
        assert len(j["symbols"]) >= 1
        for row in j["symbols"]:
            assert "win_rate" in row
            # Some CI keys expected — accept ci_low/ci_high naming
            has_ci = ("ci_low" in row and "ci_high" in row) or "ci" in row
            assert has_ci, f"symbol row missing CI: {row}"

        # walk_forward weekly list
        assert isinstance(j["walk_forward"], list)
        if j["walk_forward"]:
            wk = j["walk_forward"][0]
            for k in ("total_pnl", "win_rate", "cum_pnl"):
                assert k in wk, f"walk_forward row missing {k}"

        # regimes: expect LOW_VOL_TREND with n > 400
        assert isinstance(j["regimes"], list)
        low_vol_trend = [r for r in j["regimes"]
                         if str(r.get("regime", "")).upper() == "LOW_VOL_TREND"]
        assert low_vol_trend, f"LOW_VOL_TREND missing from regimes: {j['regimes']}"
        assert low_vol_trend[0].get("n", 0) > 400, \
            f"LOW_VOL_TREND n={low_vol_trend[0].get('n')} not > 400"

        # decay
        for k in ("status", "recent_30d_avg_pnl", "weekly_slope"):
            assert k in j["decay"], f"decay missing {k}"

        # execution
        exe = j["execution"]
        for k in ("deals", "gross_pnl", "commission", "swap",
                  "net_pnl", "cost_drag_pct"):
            assert k in exe, f"execution missing {k}"

    def test_research_days_7(self, sess):
        r = sess.get(f"{BASE}/api/analytics/research?days=7", timeout=30)
        assert r.status_code == 200, r.text
        assert "calibration" in r.json()

    def test_research_days_clamped(self, sess):
        r = sess.get(f"{BASE}/api/analytics/research?days=9999", timeout=30)
        assert r.status_code == 200, r.text
        assert "trades" in r.json()


# ---------------- BACKEND 2: /api/scalp/review
class TestScalpReview:
    def test_scalp_review(self, sess):
        r = sess.get(f"{BASE}/api/scalp/review", timeout=30)
        assert r.status_code == 200, r.text
        j = r.json()

        for k in ("heatmap", "gate_effectiveness", "cost_attribution",
                  "latency", "brokers"):
            assert k in j, f"missing key {k}"

        # heatmap: list of {dow,hour,n,labeled,avg_net_pips}
        assert isinstance(j["heatmap"], list)
        if j["heatmap"]:
            cell = j["heatmap"][0]
            for k in ("dow", "hour", "n", "labeled", "avg_net_pips"):
                assert k in cell, f"heatmap cell missing {k}"

        # gate_effectiveness: expect a 'permission' stage
        assert isinstance(j["gate_effectiveness"], list)
        stages = [str(g.get("stage", "")).lower() for g in j["gate_effectiveness"]]
        assert "permission" in stages, f"permission stage missing: {stages}"
        for g in j["gate_effectiveness"]:
            assert "stage" in g and "n" in g and "avoided_pips" in g

        # cost_attribution
        ca = j["cost_attribution"]
        for k in ("gross_pips", "spread_pips", "slippage_pips",
                  "commission_pips", "net_pips", "labeled"):
            assert k in ca, f"cost_attribution missing {k}"

        # latency
        lat = j["latency"]
        # accept either 'p95' short or 'time_to_exit_p95_s' full form
        assert "time_to_exit_p50_s" in lat
        assert "time_to_exit_p95_s" in lat or "p95" in lat

        # brokers
        assert isinstance(j["brokers"], list)
        # Expect at least one broker entry
        if j["brokers"]:
            for b in j["brokers"]:
                # accept various naming — must have some 'broker' key
                assert any(k in b for k in ("broker", "name")), b


# ---------------- BACKEND 3: reconciliation + broker_error
class TestScalpExecutionsReconciliation:
    def test_reconciliation_field(self, sess):
        r = sess.get(f"{BASE}/api/scalp/executions?limit=20", timeout=20)
        assert r.status_code == 200, r.text
        j = r.json()
        assert "items" in j
        # if any items, each must have 'reconciliation' key
        for item in j["items"]:
            assert "reconciliation" in item, f"missing reconciliation: {item}"


class TestAuditBrokerError:
    def test_execution_summary_broker_error(self, sess):
        trade_id = "6a612a5b6936705cbd7c78f3"
        r = sess.get(f"{BASE}/api/trades/{trade_id}/audit", timeout=20)
        assert r.status_code == 200, r.text
        j = r.json()
        assert "execution_summary" in j
        es = j["execution_summary"]
        assert "broker_error" in es, f"broker_error missing from execution_summary: {list(es.keys())}"


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
