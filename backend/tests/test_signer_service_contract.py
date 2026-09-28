"""deploy/signer/app.py must satisfy the contract backend/release_signing.py
relies on (external mode): /sign, /public-key, authenticated /health identity
(signer_health), /healthz liveness. Runs the signer in-process with a
throwaway key, so no network and no real secret."""
import base64
import importlib.util
import os
import sys

import pytest
from cryptography.hazmat.primitives import serialization as s
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "backend"))
import release_signing as rs  # noqa: E402

pytestmark = pytest.mark.unit

TOKEN = "test-signer-bearer-" + "x" * 16


@pytest.fixture(scope="module")
def signer():
    k = Ed25519PrivateKey.generate()
    priv = base64.b64encode(k.private_bytes(s.Encoding.Raw, s.PrivateFormat.Raw, s.NoEncryption())).decode()
    pub = base64.b64encode(k.public_key().public_bytes(s.Encoding.Raw, s.PublicFormat.Raw)).decode()
    os.environ["SIGNER_TOKEN"] = TOKEN
    os.environ["ED25519_SIGNING_KEY_B64"] = priv
    spec = importlib.util.spec_from_file_location(
        "stoic_signer_app", os.path.join(ROOT, "deploy", "signer", "app.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    os.environ.pop("ED25519_SIGNING_KEY_B64", None)
    return TestClient(mod.app), pub


def test_healthz_is_unauthenticated(signer):
    c, _ = signer
    assert c.get("/healthz").json() == {"status": "ok"}


def test_public_key_matches_generated(signer):
    c, pub = signer
    body = c.get("/public-key").json()
    assert body == {"key_id": rs.KEY_ID, "public_key_b64": pub}


def test_health_identity_requires_bearer_and_matches_signer_health_shape(signer):
    c, pub = signer
    assert c.get("/health").status_code == 401
    assert c.get("/health", headers={"Authorization": "Bearer wrong"}).status_code == 403
    body = c.get("/health", headers={"Authorization": f"Bearer {TOKEN}"}).json()
    assert body == {"ok": True, "key_id": rs.KEY_ID, "public_key_b64": pub}


def test_sign_verifies_against_pinned_key_and_rejects_bad_inputs(signer):
    c, pub = signer
    data = b"stoic-release-canary"
    h = {"Authorization": f"Bearer {TOKEN}"}
    r = c.post("/sign", json={"key_id": rs.KEY_ID, "data_hex": data.hex()}, headers=h)
    assert r.status_code == 200
    sig = r.json()["signature_hex"]
    assert len(sig) == 128 and r.json()["key_id"] == rs.KEY_ID
    assert rs.verify_hex(data, sig, pub)
    assert not rs.verify_hex(data + b"x", sig, pub)
    assert c.post("/sign", json={"key_id": rs.KEY_ID, "data_hex": data.hex()}).status_code == 401
    assert c.post("/sign", json={"key_id": "other", "data_hex": data.hex()}, headers=h).status_code == 400
    assert c.post("/sign", json={"key_id": rs.KEY_ID, "data_hex": "zz"}, headers=h).status_code == 400


def test_backend_signer_health_accepts_the_signer(signer, monkeypatch):
    c, pub = signer
    import requests

    class _Resp:
        def __init__(self, r):
            self._r = r
            self.status_code = r.status_code
        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(str(self.status_code))
        def json(self):
            return self._r.json()

    def fake_get(url, headers=None, timeout=None):
        return _Resp(c.get(url.replace("https://signer.example", ""), headers=headers))

    monkeypatch.setattr(requests, "get", fake_get)
    env = {"APP_ENV": "production", "RELEASE_SIGNER": "external",
           "RELEASE_SIGNER_URL": "https://signer.example", "RELEASE_SIGNER_ALLOWED_HOSTS": "signer.example",
           "RELEASE_SIGNER_TOKEN": TOKEN, "RELEASE_SIGNER_KEY_ID": rs.KEY_ID,
           "RELEASE_PUBLIC_KEY_B64": pub, "RELEASE_SIGNER_TIMEOUT": "5"}
    out = rs.signer_health(env)
    assert out["ok"] is True and out["identity_matches"] is True
    other = base64.b64encode(b"\x01" * 32).decode()
    assert rs.signer_health({**env, "RELEASE_PUBLIC_KEY_B64": other})["ok"] is False
