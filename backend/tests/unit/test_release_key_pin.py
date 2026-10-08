"""v1.60.7 — release-purpose verification never falls back to the runtime/bundle key when
RELEASE_PUBLIC_KEY_B64 is unpinned, and the operator gets a clear message instead of
"Ed25519 signature does NOT verify"."""
import base64
import importlib
import os
import sys

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _keypair():
    k = Ed25519PrivateKey.generate()
    pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return k, base64.b64encode(pub).decode()


def _env(monkeypatch, **kw):
    for k in ("RELEASE_PUBLIC_KEY_B64", "BUNDLE_PUBLIC_KEY_B64", "RELEASE_SIGNER", "ED25519_SIGNING_KEY_B64",
              "RELEASE_SIGNER_URL", "RELEASE_SIGNER_DEFERRED"):
        monkeypatch.delenv(k, raising=False)
    for k, v in kw.items():
        monkeypatch.setenv(k, v)


def test_external_mode_unpinned_ea_release_never_uses_bundle_or_runtime_key(monkeypatch):
    import release_signing as rs
    runtime, runtime_pub = _keypair()
    _env(monkeypatch, RELEASE_SIGNER="external", RELEASE_SIGNER_URL="https://signer:9443",
         BUNDLE_PUBLIC_KEY_B64=runtime_pub,
         ED25519_SIGNING_KEY_B64=base64.b64encode(runtime.private_bytes(
             serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())).decode())
    body = b"ea-record"
    sig = runtime.sign(rs.domain_bytes("ea-release", body)).hex()        # signed by the RUNTIME key
    assert rs.verify_hex(body, sig, purpose="ea-release") is False       # must NOT verify against the bundle/runtime key
    assert rs.verify_hex(body, sig, purpose="model-manifest") is False
    with pytest.raises(rs.ReleaseKeyNotPinned) as ei:
        rs.public_key_b64("ea-release")
    assert "RELEASE_PUBLIC_KEY_B64 not pinned" in str(ei.value) and "deploy/update.sh" in str(ei.value)
    assert rs.release_key_pinned() is False
    # runtime purposes keep working through the bundle pin
    assert rs.verify_hex(body, runtime.sign(rs.domain_bytes("acceptance-bundle", body)).hex(), purpose="acceptance-bundle")


def test_pinned_ci_key_verifies_ea_release(monkeypatch):
    import release_signing as rs
    ci, ci_pub = _keypair()
    _env(monkeypatch, RELEASE_SIGNER="external", RELEASE_SIGNER_URL="https://signer:9443", RELEASE_PUBLIC_KEY_B64=ci_pub)
    body = b"ea-record"
    assert rs.verify_hex(body, ci.sign(rs.domain_bytes("ea-release", body)).hex(), purpose="ea-release") is True
    assert rs.release_key_pinned() is True


def test_local_mode_keeps_developer_self_verification(monkeypatch):
    import release_signing as rs
    k, _ = _keypair()
    _env(monkeypatch, RELEASE_SIGNER="local", ED25519_SIGNING_KEY_B64=base64.b64encode(k.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())).decode())
    body = b"dev"
    assert rs.verify_hex(body, k.sign(rs.domain_bytes("ea-release", body)).hex(), purpose="ea-release") is True


def test_check_entry_reports_unpinned_key_not_bad_signature(monkeypatch):
    _env(monkeypatch, RELEASE_SIGNER="external", RELEASE_SIGNER_URL="https://signer:9443")
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    ver = importlib.import_module("verify_ea_release")
    ea = {"ex5_sha256": "a" * 64, "compiled_by": "github-actions", "compile_log": {"errors": 0},
          "signature": {"sig_hex": "00" * 64, "key_id": "stoic-release-ed25519-v1"}}
    monkeypatch.setattr(ver.os.path, "exists", lambda p: False)
    fails = ver.check_entry(ea)
    assert any("RELEASE_PUBLIC_KEY_B64 not pinned" in f for f in fails)
    assert not any("does NOT verify" in f for f in fails)
