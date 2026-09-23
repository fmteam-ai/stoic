from live_target import ADMIN_EMAIL, ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
"""Iter-61 HTTP verification of /api/scalp/exec-calibration and
/api/scalp/broker-stats. Uses admin login + CSRF + rate-limit bypass."""
import os

import pytest
import requests

from live_target import require_live_base_url
BASE_URL = require_live_base_url()


@pytest.fixture(scope="module")
def admin_client():
    s = requests.Session()
    # Login sets httpOnly access_token cookie
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
               timeout=15)
    assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text}"
    return s


class TestExecCalibration:
    def test_endpoint_returns_expected_shape(self, admin_client):
        r = admin_client.get(f"{BASE_URL}/api/scalp/exec-calibration", timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "buckets" in body and "thresholds" in body
        # Exactly 4 buckets in canonical order
        assert [b["bucket"] for b in body["buckets"]] == [
            "0-39", "40-59", "60-79", "80-100"]
        # Row fields
        for row in body["buckets"]:
            for k in ("n", "live_submitted", "broker_rejected",
                      "history_capped", "labeled", "avg_net_pips",
                      "target_first_rate", "reject_rate"):
                assert k in row, f"missing {k} in bucket row {row}"
        # Thresholds snapshot
        th = body["thresholds"]
        assert th["version"] == 1
        assert th["exec_quality_min"] == 40
        assert th["min_broker_fills_for_live"] == 20
        assert "weights" in th and th["weights"]["spread"] == 22

    def test_requires_auth(self):
        r = requests.get(f"{BASE_URL}/api/scalp/exec-calibration", timeout=15)
        assert r.status_code in (401, 403), r.status_code


class TestBrokerStats:
    def test_returns_fills_fields(self, admin_client):
        r = admin_client.get(f"{BASE_URL}/api/scalp/broker-stats?broker=OnEquity",
                             timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "totals" in body and "sessions" in body
        assert "fills" in body["totals"]  # per iter-61 fills aggregate
        # sessions dict may be empty for a fresh broker but structure must hold
        for sess in body["sessions"].values():
            assert "fills" in sess

    def test_requires_auth(self):
        r = requests.get(f"{BASE_URL}/api/scalp/broker-stats?broker=OnEquity",
                         timeout=15)
        assert r.status_code in (401, 403), r.status_code


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
