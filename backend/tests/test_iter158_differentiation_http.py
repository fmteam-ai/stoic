"""iter-158 HTTP integration tests — Phase 8/9 differentiation endpoints.

Covers:
  GET  /api/performance/verified            (auth, attestation attached)
  POST /api/public/performance/verify       (no auth, valid + tampered)
  POST /api/performance/share -> GET /api/public/performance/{share_id} -> DELETE
  GET  /api/performance/evidence            (auth, 6 features, days clamp)
  GET  /api/broker-intel                    (auth, certification per row)
  GET  /api/broker-intel/certification      (auth, brokers + tiers legend)
"""
import os
import re

import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL",
                          "https://stoic-trading-bot.preview.emergentagent.com").rstrip("/")
ADMIN = {"email": "admin@trading.bot", "password": "admin123"}
HEX64 = re.compile(r"^[0-9a-f]{64}$")


@pytest.fixture(scope="module")
def sess():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login", json=ADMIN, timeout=15)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return s


# ------------------------------------------------------------ attestation
class TestAttestation:
    def test_verified_returns_attestation(self, sess):
        r = sess.get(f"{BASE_URL}/api/performance/verified", timeout=15)
        assert r.status_code == 200, r.text
        j = r.json()
        # Regression: core stats present
        for k in ("overall", "equity_curve", "accounts", "integrity",
                  "attestation", "generated_at"):
            assert k in j, f"missing {k}"
        assert isinstance(j["equity_curve"], list)
        assert isinstance(j["accounts"], list)
        assert isinstance(j["integrity"], dict)
        assert j["integrity"]["source"] == "broker_deals"

        att = j["attestation"]
        assert att["key_id"] == "perf-hmac-v1"
        assert att["algo"] == "HMAC-SHA256(canonical-JSON)"
        assert HEX64.match(att["payload_hash"]), att["payload_hash"]
        assert HEX64.match(att["signature"]), att["signature"]
        assert "signed_at" in att

    def test_public_verify_valid(self, sess):
        att = sess.get(f"{BASE_URL}/api/performance/verified",
                       timeout=15).json()["attestation"]
        # No auth — plain requests
        r = requests.post(f"{BASE_URL}/api/public/performance/verify",
                          json={"payload_hash": att["payload_hash"],
                                "signature": att["signature"]}, timeout=15)
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["valid"] is True
        assert j["key_id"] == "perf-hmac-v1"

    def test_public_verify_tampered_hash(self, sess):
        att = sess.get(f"{BASE_URL}/api/performance/verified",
                       timeout=15).json()["attestation"]
        bad_hash = "f" * 64
        r = requests.post(f"{BASE_URL}/api/public/performance/verify",
                          json={"payload_hash": bad_hash,
                                "signature": att["signature"]}, timeout=15)
        assert r.status_code == 200
        assert r.json()["valid"] is False

    def test_public_verify_wrong_signature(self, sess):
        att = sess.get(f"{BASE_URL}/api/performance/verified",
                       timeout=15).json()["attestation"]
        r = requests.post(f"{BASE_URL}/api/public/performance/verify",
                          json={"payload_hash": att["payload_hash"],
                                "signature": "0" * 64}, timeout=15)
        assert r.status_code == 200
        assert r.json()["valid"] is False

    def test_public_verify_requires_no_auth(self):
        # Fresh session (no cookies) still works
        r = requests.post(f"{BASE_URL}/api/public/performance/verify",
                          json={"payload_hash": "a" * 64,
                                "signature": "b" * 64}, timeout=15)
        assert r.status_code == 200


# ------------------------------------------------------------ share flow
class TestShareFlow:
    def test_share_lifecycle_with_attestation(self, sess):
        # Cleanup any pre-existing share
        sess.delete(f"{BASE_URL}/api/performance/share", timeout=15)

        # CREATE share
        r = sess.post(f"{BASE_URL}/api/performance/share", timeout=15)
        assert r.status_code == 200, r.text
        share_id = r.json()["share_id"]
        assert isinstance(share_id, str) and len(share_id) > 10

        # PUBLIC read (no auth)
        r = requests.get(f"{BASE_URL}/api/public/performance/{share_id}",
                         timeout=15)
        assert r.status_code == 200, r.text
        j = r.json()
        assert j.get("shared") is True
        assert "attestation" in j
        att = j["attestation"]
        assert HEX64.match(att["payload_hash"])
        assert HEX64.match(att["signature"])
        # Verify the masked payload's attestation
        v = requests.post(f"{BASE_URL}/api/public/performance/verify",
                          json={"payload_hash": att["payload_hash"],
                                "signature": att["signature"]}, timeout=15)
        assert v.json()["valid"] is True
        # Masked labels: no leaked account labels; ACCOUNT-N
        for acc in j.get("accounts", []):
            assert acc["label"].startswith("ACCOUNT-"), acc["label"]

        # REVOKE
        r = sess.delete(f"{BASE_URL}/api/performance/share", timeout=15)
        assert r.status_code == 200
        assert r.json()["revoked"] in (True, False)  # may be false if none

        # After revoke -> 404
        r = requests.get(f"{BASE_URL}/api/public/performance/{share_id}",
                         timeout=15)
        assert r.status_code == 404

    def test_public_unknown_share_404(self):
        r = requests.get(
            f"{BASE_URL}/api/public/performance/does-not-exist-xyz",
            timeout=15)
        assert r.status_code == 404


# ------------------------------------------------------------ evidence
class TestEvidence:
    def test_evidence_shape(self, sess):
        r = sess.get(f"{BASE_URL}/api/performance/evidence", timeout=15)
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["window_days"] == 30
        assert "principle" in j and isinstance(j["principle"], str)
        feats = j["features"]
        assert len(feats) == 6
        keys = {f["feature"] for f in feats}
        assert keys == {"execution_timing", "adaptive_exits", "regime_gating",
                        "dynamic_allocation", "risk_layers", "learning_pipeline"}
        for f in feats:
            assert f["verdict"] in {"proven", "experimental", "review"}
            assert "n" in f and "metric" in f and "question" in f
        s = j["summary"]
        assert s["proven"] + s["experimental"] + s["review"] == 6

    def test_evidence_days_clamp_low(self, sess):
        r = sess.get(f"{BASE_URL}/api/performance/evidence?days=0", timeout=15)
        assert r.status_code == 200
        assert r.json()["window_days"] == 1

    def test_evidence_days_clamp_high(self, sess):
        r = sess.get(f"{BASE_URL}/api/performance/evidence?days=99999",
                     timeout=15)
        assert r.status_code == 200
        assert r.json()["window_days"] == 365

    def test_evidence_requires_auth(self):
        r = requests.get(f"{BASE_URL}/api/performance/evidence", timeout=15)
        assert r.status_code in (401, 403)


# ------------------------------------------------------------ certification
class TestBrokerCertification:
    def test_broker_intel_has_certification(self, sess):
        r = sess.get(f"{BASE_URL}/api/broker-intel", timeout=20)
        assert r.status_code == 200, r.text
        j = r.json()
        assert "brokers" in j
        for b in j["brokers"]:
            assert "certification" in b, f"missing certification on {b}"
            c = b["certification"]
            assert c["tier"] in {"CERTIFIED", "ACCEPTABLE", "DEGRADED",
                                 "PROVISIONAL"}
            assert isinstance(c.get("detail"), str)

    def test_certification_endpoint(self, sess):
        r = sess.get(f"{BASE_URL}/api/broker-intel/certification", timeout=20)
        assert r.status_code == 200, r.text
        j = r.json()
        assert "brokers" in j and "tiers" in j
        assert set(j["tiers"].keys()) == {"CERTIFIED", "ACCEPTABLE",
                                          "DEGRADED", "PROVISIONAL"}
        for b in j["brokers"]:
            assert "certification" in b
            assert b["certification"]["tier"] in j["tiers"]

    def test_broker_intel_requires_auth(self):
        r = requests.get(f"{BASE_URL}/api/broker-intel", timeout=15)
        assert r.status_code in (401, 403)
        r = requests.get(f"{BASE_URL}/api/broker-intel/certification",
                         timeout=15)
        assert r.status_code in (401, 403)


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
