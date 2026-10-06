"""N100-11 — signer separation: domain-prefixed signatures + per-purpose tokens; the trading API can never
obtain an EA-release signature."""
import base64
import importlib.util
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _signer_env():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization as s
    k = Ed25519PrivateKey.generate()
    priv = base64.b64encode(k.private_bytes(s.Encoding.Raw, s.PrivateFormat.Raw, s.NoEncryption())).decode()
    pub = base64.b64encode(k.public_key().public_bytes(s.Encoding.Raw, s.PublicFormat.Raw)).decode()
    return {"RELEASE_SIGNER": "local", "APP_ENV": "preview", "ED25519_SIGNING_KEY_B64": priv,
            "RELEASE_PUBLIC_KEY_B64": pub, "RELEASE_SIGNER_KEY_ID": "stoic-release-ed25519-v1"}, priv, pub


def test_domain_prefix_makes_purposes_mutually_unverifiable():
    import release_signing as rs
    env, _, _ = _signer_env()
    with patch.dict(os.environ, env):
        sig = rs.sign_hex(b"payload", purpose="acceptance-bundle")
        assert rs.verify_hex(b"payload", sig, purpose="acceptance-bundle")
        assert not rs.verify_hex(b"payload", sig, purpose="ea-release")
        assert not rs.verify_hex(b"payload", sig, purpose="model-manifest")
        with pytest.raises(ValueError):
            rs.sign_hex(b"x", purpose="made-up")
    assert rs.RELEASE_PURPOSES == {"ea-release", "model-manifest"} and "acceptance-bundle" in rs.API_PURPOSES


def test_api_uses_bundle_token_for_runtime_purposes_and_release_token_only_for_release():
    import release_signing as rs
    env = {"RELEASE_SIGNER_TOKEN": "rel", "RELEASE_SIGNER_BUNDLE_TOKEN": "bun"}
    assert rs._token_for("acceptance-bundle", env) == "bun" and rs._token_for("audit-anchor", env) == "bun"
    assert rs._token_for("ea-release", env) == "rel"
    assert rs._token_for("acceptance-bundle", {"RELEASE_SIGNER_TOKEN": "rel"}) == "rel"   # single-token install


def test_signer_sidecar_refuses_release_purposes_with_the_bundle_token(tmp_path):
    env, priv, pub = _signer_env()
    with patch.dict(os.environ, {"SIGNER_TOKEN": "rel-token", "SIGNER_TOKEN_BUNDLE": "bundle-token",
                                 "ED25519_SIGNING_KEY_B64": priv, "SIGNER_KEY_ID": "stoic-release-ed25519-v1"}):
        spec = importlib.util.spec_from_file_location("signer_app", os.path.join(ROOT, "deploy", "signer", "app.py"))
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        from fastapi.testclient import TestClient
        c = TestClient(mod.app)
        body = {"key_id": mod.KEY_ID, "data_hex": b"rec".hex(), "purpose": "ea-release"}
        assert c.post("/sign", json=body, headers={"Authorization": "Bearer bundle-token"}).status_code == 403
        assert c.post("/sign", json={**body, "purpose": "acceptance-bundle"}, headers={"Authorization": "Bearer rel-token"}).status_code == 403
        assert c.post("/sign", json={**body, "purpose": "nope"}, headers={"Authorization": "Bearer rel-token"}).status_code == 400
        ok = c.post("/sign", json=body, headers={"Authorization": "Bearer rel-token"})
        assert ok.status_code == 200
        import release_signing as rs
        assert rs.verify_hex(b"rec", ok.json()["signature_hex"], pub, purpose="ea-release")
        assert not rs.verify_hex(b"rec", ok.json()["signature_hex"], pub, purpose="acceptance-bundle")
        okb = c.post("/sign", json={**body, "purpose": "acceptance-bundle"}, headers={"Authorization": "Bearer bundle-token"})
        assert okb.status_code == 200 and rs.verify_hex(b"rec", okb.json()["signature_hex"], pub, purpose="acceptance-bundle")
    assert mod.PURPOSES == rs.PURPOSES   # byte-for-byte mirror


def test_compose_never_hands_the_release_token_to_app_containers():
    import yaml
    c = yaml.safe_load(open(os.path.join(ROOT, "docker-compose.yml")))
    holders = [n for n, svc in c["services"].items() if "signer_token" in (svc.get("secrets") or [])]
    assert holders == ["signer"]
    assert "signer_token_bundle" in c["secrets"]
    lib = open(os.path.join(ROOT, "deploy", "lib.sh")).read()
    assert "secrets/signer_token_bundle" in lib
    assert "signer_token_bundle" in open(os.path.join(ROOT, "deploy", "install.sh")).read()
