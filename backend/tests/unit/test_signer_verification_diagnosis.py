"""ea-release CI failure 'external signer signature failed local verification' must name its cause:
a signer still running pre-N100-11 code (raw-bytes signature) or a wrong public-key pin."""
import base64
import os
import re

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _pub_b64(key):
    return base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()


@pytest.fixture
def ext(monkeypatch):
    import release_signing
    signer = Ed25519PrivateKey.generate()
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("RELEASE_SIGNER", "external")
    monkeypatch.setenv("RELEASE_SIGNER_URL", "https://signer.internal")
    monkeypatch.setenv("RELEASE_SIGNER_ALLOWED_HOSTS", "signer.internal")
    monkeypatch.setenv("RELEASE_SIGNER_TOKEN", "tok")
    monkeypatch.setenv("RELEASE_SIGNER_KEY_ID", release_signing.KEY_ID)
    monkeypatch.setenv("RELEASE_PUBLIC_KEY_B64", _pub_b64(signer))
    monkeypatch.setenv("RELEASE_SIGNER_TIMEOUT", "5")
    monkeypatch.delenv("ED25519_SIGNING_KEY_B64", raising=False)
    monkeypatch.delenv("RELEASE_SIGNER_DEFERRED", raising=False)
    return release_signing, signer


class _Resp:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


def test_old_signer_code_without_domain_prefix_is_named(ext, monkeypatch):
    rs, signer = ext
    import requests
    monkeypatch.setattr(requests, "post", lambda url, json=None, **k:
                        _Resp({"signature_hex": signer.sign(bytes.fromhex(json["data_hex"])).hex()}))
    with pytest.raises(RuntimeError, match="pre-N100-11 code.*flyctl deploy") as ei:
        rs.sign_hex(b"ea-record", purpose="ea-release")
    assert "failed local verification" in str(ei.value)


def test_wrong_pin_reports_served_vs_pinned_key(ext, monkeypatch):
    rs, signer = ext
    import requests
    other = Ed25519PrivateKey.generate()          # the key the signer REALLY holds
    monkeypatch.setattr(requests, "post", lambda url, json=None, **k:
                        _Resp({"signature_hex": other.sign(rs.domain_bytes("ea-release", bytes.fromhex(json["data_hex"]))).hex()}))
    monkeypatch.setattr(requests, "get", lambda url, **k: _Resp({"key_id": rs.KEY_ID, "public_key_b64": _pub_b64(other)}))
    with pytest.raises(RuntimeError, match=re.escape(f"serves public key {_pub_b64(other)}")) as ei:
        rs.sign_hex(b"ea-record", purpose="ea-release")
    assert _pub_b64(signer) in str(ei.value) and "RELEASE_PUBLIC_KEY_B64" in str(ei.value)


def test_diagnosis_survives_unreachable_public_key_endpoint(ext, monkeypatch):
    rs, _ = ext
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp({"signature_hex": "00" * 64}))

    def boom(*a, **k):
        raise requests.ConnectionError("down")
    monkeypatch.setattr(requests, "get", boom)
    with pytest.raises(RuntimeError, match="failed local verification.*unreachable"):
        rs.sign_hex(b"x", purpose="ea-release")


def test_correct_signature_still_passes(ext, monkeypatch):
    rs, signer = ext
    import requests
    monkeypatch.setattr(requests, "post", lambda url, json=None, **k:
                        _Resp({"signature_hex": signer.sign(rs.domain_bytes("ea-release", bytes.fromhex(json["data_hex"]))).hex()}))
    sig = rs.sign_hex(b"ok", purpose="ea-release")
    assert rs.verify_hex(b"ok", sig, purpose="ea-release")


def test_ea_release_workflow_probes_signer_before_mt5_install():
    wf = open(os.path.join(ROOT, ".github", "workflows", "ea-release.yml")).read()
    assert "Signer identity preflight" in wf and "scripts/signer_probe.py" in wf
    assert wf.index("Signer secrets present") < wf.index("Signer identity preflight") < wf.index("Install MetaTrader 5")
    doc = open(os.path.join(ROOT, "docs", "RELEASE_SIGNER.md")).read()
    assert "failed local verification against the pinned public key" in doc and "pre-N100-11" in doc


def test_checklist_pins_the_key_the_fly_signer_serves():
    doc = open(os.path.join(ROOT, "docs", "PRODUCTION_DEPLOY_CHECKLIST.md")).read()
    assert "1NgD7Rq2/8Fa31kwU2N18krBt3d5zPkwmg60MUW0Gkc=" in doc
    assert "/lYiSnGAY8/nWkdSKoGSXUJtDm0Syd+ioKTFa+s7cCo=" not in doc
