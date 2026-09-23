from live_target import ADMIN_EMAIL, ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
"""iter-163 live-stack verification of P0/P1 fixes (review request).

Covers:
* /api/ops/canary/status → dimensions[] + thresholds.boundary_rule
* /api/ops/canary/evaluate returns dimensions
* /api/performance/verified → attestation OR attestation_blocked
* /api/public/performance/verify → legacy_hmac_accepted_until
Leaves canary DISABLED at end (finally-block).
"""
import os
import pytest
import requests

from tests.live_target import require_live_base_url

BASE = require_live_base_url()
CANARY_ACCT = "6a39653e0760995b7e966183"


@pytest.fixture(scope="module")
def admin():
    s = requests.Session()
    r = s.post(f"{BASE}/api/auth/login",
               json={"email": "admin@stoicaibot.com", "password": ADMIN_PASSWORD},
               timeout=15)
    if r.status_code != 200:
        pytest.skip(f"admin login failed: {r.status_code} {r.text[:200]}")
    return s


def test_canary_status_and_evaluate_multidim(admin):
    # Ensure clean start
    admin.post(f"{BASE}/api/ops/canary/disable", timeout=10)
    try:
        r = admin.post(f"{BASE}/api/ops/canary/enable",
                       json={"account_id": CANARY_ACCT}, timeout=15)
        assert r.status_code in (200, 201), r.text
        st = admin.get(f"{BASE}/api/ops/canary/status", timeout=15).json()
        assert st.get("enabled") is True
        dims = st.get("dimensions")
        assert isinstance(dims, list) and len(dims) >= 2
        names = {d.get("dimension") for d in dims}
        assert {"guard_block_rate", "execution_failure_rate"} <= names
        for d in dims:
            for k in ("judged", "diverged", "reason", "rates"):
                assert k in d, f"{d['dimension']} missing {k}"
        thr = st.get("thresholds") or {}
        assert thr.get("boundary_rule") == "stricter of +25pp / 3× fleet"
        assert thr.get("min_canary_executions") == 10

        ev = admin.post(f"{BASE}/api/ops/canary/evaluate", timeout=15)
        assert ev.status_code == 200, ev.text
        body = ev.json()
        assert isinstance(body.get("dimensions"), list) and \
            len(body["dimensions"]) >= 2
    finally:
        admin.post(f"{BASE}/api/ops/canary/disable", timeout=10)
        final = admin.get(f"{BASE}/api/ops/canary/status", timeout=10).json()
        assert final.get("enabled") is False, "canary must be DISABLED at end"


def test_performance_verified_attestation_and_public_verify(admin):
    r = admin.get(f"{BASE}/api/performance/verified", timeout=15)
    assert r.status_code == 200, r.text
    body = r.json()
    # Either attestation present OR attestation_blocked present
    attn = body.get("attestation")
    blk = body.get("attestation_blocked")
    assert (attn is None) or isinstance(attn, dict)
    assert (blk is None) or isinstance(blk, dict)
    if attn:
        assert attn.get("key_id") == "perf-ed25519-v1"
        assert attn.get("algo", "").lower().startswith("ed25519")
        ph = attn.get("payload_hash")
        sig = attn.get("signature")
        assert ph and sig
        vr = requests.post(
            f"{BASE}/api/public/performance/verify",
            json={"payload_hash": ph, "signature": sig,
                  "key_id": attn.get("key_id"),
                  "algo": attn.get("algo")},
            timeout=15)
        assert vr.status_code == 200, vr.text
        vb = vr.json()
        assert vb.get("valid") is True, vb
        assert vb.get("legacy_hmac_accepted_until") == \
            "2027-01-01T00:00:00+00:00"
    else:
        assert blk, "either attestation or attestation_blocked must be set"
        assert "reasons" in blk


pytestmark = pytest.mark.http
