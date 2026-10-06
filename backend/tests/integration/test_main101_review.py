"""Main101 review backend integration tests (iter 239).

Scope — N101 deltas from the STOIC main101 Review:
- GET /api/admin/release-gate → `accounts[]` with per-account allowed/reason (N101-2).
- GET /api/infra/artifacts/manifest → signature.purpose='artifact-manifest',
  key_id present, 128 hex value, Ed25519 verifies with
  b'stoic:artifact-manifest:v1\\0' and DOES NOT verify with the EA-release prefix (N101-5).
- GET /api/health/release → release_identity.ea_signed_record is boolean,
  response has image_digest + ea_shipped_version='1.60' (N101-7).
- GET /api/ops/deploy-preflight → includes release_signer_token_scope=pass (N101-5).
- POST /api/performance/attestation/verify → still returns domain_prefix
  'stoic:differentiation:v1\\0' + public_key_b64.
- Acceptance bundle flow: GET /api/admin/acceptance/current 200; POST
  /api/admin/acceptance/bundle must not 500 (step-up 401/403 is OK).

Credentials: TEST_ADMIN_EMAIL / TEST_ADMIN_PASSWORD env — never in source.
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))

import base64
import binascii
import json
import os
import re

import pytest
import requests

from live_target import require_live_base_url, resolve_admin_credentials  # noqa: E402

pytestmark = pytest.mark.integration

BASE_URL = require_live_base_url()
ADMIN_EMAIL, ADMIN_PASSWORD = resolve_admin_credentials()


# ───────── helpers / fixtures ─────────
def _login(session, email, password):
    return session.post(f"{BASE_URL}/api/auth/login",
                        json={"email": email, "password": password}, timeout=15)


def _csrf_headers(session):
    tok = session.cookies.get("csrf_token")
    return {"X-CSRF-Token": tok} if tok else {}


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = _login(s, ADMIN_EMAIL, ADMIN_PASSWORD)
    if r.status_code != 200:
        pytest.skip(f"admin login failed: {r.status_code} {r.text[:200]}")
    me = s.get(f"{BASE_URL}/api/auth/me", timeout=10)
    assert me.status_code == 200, me.text
    assert me.json().get("role") == "admin", me.json()
    return s


# ───────── N101-2 — admin/release-gate accounts[] ─────────
class TestReleaseGateAccounts:
    def test_release_gate_shape_and_accounts(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/admin/release-gate", timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        for k in ("enforced", "ok", "failures", "lock_commit", "accounts"):
            assert k in body, f"missing key {k!r} in {list(body)}"
        # Preview: enforced is False
        assert body["enforced"] is False, body
        assert isinstance(body["failures"], list), body
        assert isinstance(body["accounts"], list), body
        # Verify each account row shape; in preview all should be allowed with the standard reason
        for a in body["accounts"]:
            for k in ("account_id", "label", "environment", "allowed", "reason"):
                assert k in a, f"missing {k} in account row {a}"
            assert a["allowed"] is True, a
            assert a["reason"] == "release gate enforced in production only", a


# ───────── N101-5 — artifact manifest signature ─────────
class TestArtifactManifestSignature:
    def test_manifest_signature_verifies_with_artifact_manifest_domain(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/infra/artifacts/manifest", timeout=15)
        assert r.status_code == 200, r.text
        m = r.json()
        sig = m.get("signature")
        assert sig, f"no signature in manifest: {list(m)}"
        assert sig.get("alg") == "Ed25519", sig
        assert sig.get("purpose") == "artifact-manifest", sig
        assert sig.get("key_id"), sig
        value = sig.get("value") or ""
        assert re.fullmatch(r"[0-9a-f]{128}", value), f"bad hex sig: {value!r}"
        pub_b64 = sig.get("public_key_b64")
        assert pub_b64, sig

        # Reconstruct canonical body exactly like backend/vps_pathb.py:498-500
        body = json.dumps({"artifacts": m["artifacts"], "update_policy": m["update_policy"]},
                          sort_keys=True, separators=(",", ":"), default=str).encode()

        # Verify Ed25519 with the artifact-manifest domain prefix
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(pub_b64))
        sig_bytes = binascii.unhexlify(value)

        pub.verify(sig_bytes, b"stoic:artifact-manifest:v1\0" + body)

        # Must NOT verify with the EA-release domain prefix
        with pytest.raises(Exception):
            pub.verify(sig_bytes, b"stoic:ea-release:v1\0" + body)


# ───────── N101-7 — /api/health/release structure ─────────
class TestHealthRelease:
    def test_health_release_shape(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/health/release", timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        ri = body.get("release_identity")
        assert isinstance(ri, dict), body
        # N101-7 — image_digest + ea_shipped_version live under release_identity
        assert "image_digest" in ri, ri
        assert ri.get("ea_shipped_version") == "1.60", ri
        assert isinstance(ri.get("ea_signed_record"), bool), ri
        # In preview there is no signed record on disk
        assert ri["ea_signed_record"] is False, ri


# ───────── N101-5 — preflight: release_signer_token_scope=pass ─────────
class TestPreflightReleaseSignerScope:
    def _get_preflight(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/ops/deploy-preflight", timeout=20)
        if r.status_code == 403:
            # fall back to METRICS_TOKEN header (if session isn't admin for ops)
            tok = os.environ.get("METRICS_TOKEN") or ""
            if tok:
                r = requests.get(f"{BASE_URL}/api/ops/deploy-preflight",
                                 headers={"X-Metrics-Token": tok}, timeout=20)
        return r

    def test_release_signer_token_scope_present_and_pass(self, admin_session):
        r = self._get_preflight(admin_session)
        assert r.status_code == 200, f"{r.status_code} {r.text[:300]}"
        body = r.json()
        checks = body.get("checks") or body.get("results") or []
        if isinstance(body, list):
            checks = body
        # try common shapes
        if not checks and isinstance(body, dict):
            # some impls return top-level dict with 'items' or similar
            for v in body.values():
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    checks = v
                    break
        ids = [c.get("id") for c in checks if isinstance(c, dict)]
        assert "release_signer_token_scope" in ids, f"missing release_signer_token_scope; ids={ids}"
        row = next(c for c in checks if c.get("id") == "release_signer_token_scope")
        assert row.get("status") == "pass", row


# ───────── Performance attestation verify endpoint still exposes v1 domain ─────────
class TestPerformanceAttestationVerify:
    def test_domain_prefix_and_public_key(self, admin_session):
        # POST /api/performance/verify is the public verifier (dummy signature OK; we only
        # assert the stable fields domain_prefix='stoic:differentiation:v1\\0' + public_key_b64).
        r = requests.post(f"{BASE_URL}/api/public/performance/verify",
                          json={"payload_hash": "00" * 32, "signature": "00" * 64},
                          timeout=15)
        assert r.status_code == 200, f"{r.status_code} {r.text[:300]}"
        body = r.json()
        assert body.get("domain_prefix") == "stoic:differentiation:v1\0", body
        assert body.get("public_key_b64"), body
        assert "valid" in body, body


# ───────── Acceptance bundle flow — no 500 ─────────
class TestAcceptanceBundleFlow:
    def test_acceptance_current_shape(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/admin/acceptance/current", timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "accounts" in body, body
        assert isinstance(body["accounts"], list), body

    def test_acceptance_bundle_never_500(self, admin_session):
        r = admin_session.post(f"{BASE_URL}/api/admin/acceptance/bundle",
                               json={}, headers=_csrf_headers(admin_session), timeout=20)
        assert r.status_code != 500, f"{r.status_code} {r.text[:400]}"
        # Common expected outcomes: 200 (bundle), 401/403 (step-up MFA),
        # 409/412/422 (no approved inventory).
        assert r.status_code in (200, 401, 403, 409, 412, 422, 503), \
            f"unexpected {r.status_code}: {r.text[:300]}"
