"""main102 review — N102-1 (account-less release gate = evaluated per account), N102-2 (readiness projections carry
creds_version + account_trade_mode), N102-3 (model manifest + release summary adopted from signed assets),
N102-4/6 (docs), N102-5 (runtime-only sidecar, no sidecar release pin, key_id signed, preflight pins-differ)."""
import asyncio
import base64
import importlib.util
import os
import re
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _read(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _keypair():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization as s
    k = Ed25519PrivateKey.generate()
    priv = base64.b64encode(k.private_bytes(s.Encoding.Raw, s.PrivateFormat.Raw, s.NoEncryption())).decode()
    pub = base64.b64encode(k.public_key().public_bytes(s.Encoding.Raw, s.PublicFormat.Raw)).decode()
    return priv, pub


def test_n102_1_account_less_release_gate_is_full_so_user_level_authority_never_paints_demo_close_only():
    import trading_authority as ta
    bad = {"ok": False, "failures": ["rc_lock is not authoritative (developer snapshot)"], "lock_commit": None}
    with patch.dict(os.environ, {"APP_ENV": "production"}), patch("release_gate.evaluate", lambda **k: bad):
        d = _run(ta.release_gate_domain(object()))
        assert d["level"] == "FULL" and "per account" in d["reason"]
        with patch("broker_env.attested_environment", lambda a: "LIVE"):
            assert _run(ta.release_gate_domain(object(), {"_id": "l", "mode": "live"}))["level"] == "CLOSE_ONLY"
        with patch("broker_env.attested_environment", lambda a: "DEMO"):
            assert _run(ta.release_gate_domain(object(), {"_id": "d", "mode": "live"}))["level"] == "FULL"


def test_n102_2_readiness_projections_carry_identity_and_broker_mode_fields():
    for rel, anchor in (("backend/routes/admin_routes.py", "async def admin_release_gate"),
                        ("backend/demo_readiness.py", "async def fleet"),
                        ("backend/acceptance_bundle.py", "async def current_status")):
        src = _read(rel)
        body = src[src.index(anchor):src.index(anchor) + 2500]
        assert '"creds_version": 1' in body and '"account_trade_mode": 1' in body, rel
    # attested_environment really consumes both fields
    be = _read("backend/broker_env.py")
    assert "creds_version" in be and "account_trade_mode" in be


def test_n102_3_release_ships_model_manifest_and_summary_as_signed_assets_and_update_adopts_them():
    rel = _read(".github/workflows/release.yml")
    assert "cp docs/RELEASE_SUMMARY.md RELEASE_SUMMARY.md" in rel and "MODEL_MANIFEST.json" in rel
    assert "${EXTRA} > SHA256SUMS" in rel
    assets = rel[rel.index("files: |"):]
    assert "RELEASE_SUMMARY.md" in assets and "MODEL_MANIFEST.json" in assets
    lib = _read("deploy/lib.sh")
    fn = re.search(r"^adopt_release_lock\(\) \{.*?^\}", lib, re.S | re.M).group(0)
    assert "for f in MODEL_MANIFEST.json RELEASE_SUMMARY.md" in fn
    assert "backend/models_store/MODEL_MANIFEST.json" in fn and "docs/RELEASE_SUMMARY.md" in fn
    assert 'awk -v n="${f}"' in fn                      # digest taken from the SAME signed SHA256SUMS
    rst = re.search(r"^restore_tracked_release_files\(\) \{.*?^\}", lib, re.S | re.M).group(0)
    assert "backend/models_store/MODEL_MANIFEST.json" in rst and "docs/RELEASE_SUMMARY.md" in rst
    ra = re.search(r"^restore_adopted_lock\(\) \{.*?^\}", lib, re.S | re.M).group(0)
    assert "MODEL_MANIFEST-${sha}.json" in ra and "RELEASE_SUMMARY-${sha}.md" in ra
    fetch = _read("scripts/release_attestation.py")
    assert '"MODEL_MANIFEST.json", "RELEASE_SUMMARY.md"' in fetch and 'name == "MODEL_MANIFEST.json"' in fetch
    gi = _read(".gitignore")
    assert "deploy/releases/MODEL_MANIFEST-*.json" in gi and "deploy/releases/RELEASE_SUMMARY-*.md" in gi


def test_n102_4_6_docs_and_attestation_env_var():
    dep = _read("docs/DEPLOYMENT.md")
    assert "Pre-step on a host still running main100/main101" in dep and "/root/.stoic-backup-pass" in dep
    assert "ATTESTATION_REQUIRED=false" in dep and "registry mode ALWAYS requires attestation" in dep
    chk = _read("docs/PRODUCTION_DEPLOY_CHECKLIST.md")
    assert "RELEASE_SIGNER_TOKEN=<SIGNER_TOKEN>" not in chk and "RELEASE_SIGNER_BUNDLE_TOKEN=" in chk
    assert "BUNDLE_PUBLIC_KEY_B64" in chk
    lib = _read("deploy/lib.sh")
    fn = re.search(r"^attestation_required\(\) \{.*?^\}", lib, re.S | re.M).group(0)
    assert 'v="${ATTESTATION_REQUIRED:-}"' in fn       # exported variable honoured, .env line still read


def test_n102_5_runtime_sidecar_refuses_release_purposes_for_every_token():
    priv, pub = _keypair()
    env = {"SIGNER_ROLE": "runtime", "SIGNER_TOKEN_BUNDLE": "bundle-token", "SIGNER_TOKEN": "rel-token",
           "SIGNER_KEY_ID": "stoic-bundle-ed25519-v1"}
    env["ED25519_SIGNING_KEY" + "_B64"] = priv
    with patch.dict(os.environ, env, clear=False):
        spec = importlib.util.spec_from_file_location("signer_app_n102", os.path.join(ROOT, "deploy", "signer", "app.py"))
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        from fastapi.testclient import TestClient
        c = TestClient(mod.app)
        assert mod.ROLE == "runtime" and mod._token is None          # release token never loaded
        body = {"key_id": mod.KEY_ID, "data_hex": b"rec".hex()}
        for tok in ("rel-token", "bundle-token"):
            for p in ("ea-release", "model-manifest", "policy-migration"):
                assert c.post("/sign", json={**body, "purpose": p}, headers={"Authorization": f"Bearer {tok}"}).status_code == 403, (tok, p)
        ok = c.post("/sign", json={**body, "purpose": "acceptance-bundle"}, headers={"Authorization": "Bearer bundle-token"})
        assert ok.status_code == 200
        assert c.get("/health", headers={"Authorization": "Bearer bundle-token"}).json()["role"] == "runtime"
    import yaml
    comp = yaml.safe_load(_read("docker-compose.yml"))
    s = comp["services"]["signer"]
    assert s["environment"]["SIGNER_ROLE"] == "runtime" and "SIGNER_TOKEN_FILE" not in s["environment"]
    assert "signer_token" not in (s.get("secrets") or []) and "signer_token" not in comp["secrets"]
    assert s["environment"]["SIGNER_KEY_ID"] == "${SIGNER_KEY_ID:-stoic-bundle-ed25519-v1}"


def test_n102_5_release_pin_is_never_the_sidecar_key_and_preflight_flags_identical_pins():
    inst = _read("deploy/install.sh")
    assert 'set_kv backend/.env RELEASE_PUBLIC_KEY_B64 "${SIGNER_PUB_B64}"' not in inst
    assert "removed RELEASE_PUBLIC_KEY_B64 pinned to the LOCAL sidecar key" in inst
    lib = _read("deploy/lib.sh")
    fn = re.search(r"^ensure_bundle_key_pins\(\) \{.*?^\}", lib, re.S | re.M).group(0)
    # M119-1 — never silently delete the pin: refuse with the rotate command instead
    assert "equals the LOCAL sidecar key" in fn and "deploy/rotate-runtime-key.sh" in fn and "sed -i '/^RELEASE_PUBLIC_KEY_B64=/d'" not in fn
    import deploy_preflight as dp
    import release_signing as rs
    _, pub = _keypair(); _, pub2 = _keypair()
    base = {"APP_ENV": "production", "RELEASE_SIGNER": "external", "RELEASE_SIGNER_URL": "https://signer:9443",
            "RELEASE_SIGNER_ALLOWED_HOSTS": "signer", "RELEASE_SIGNER_BUNDLE_TOKEN": "b", "RELEASE_SIGNER_KEY_ID": "k"}
    # validator: the bundle pin alone is a coherent runtime configuration (release pin may be absent)
    assert rs.signer_config_violations({**base, "BUNDLE_PUBLIC_KEY_B64": pub}) == []
    assert any("pinned, valid public key" in v for v in rs.signer_config_violations(base))
    with patch.dict(os.environ, {**base, "RELEASE_PUBLIC_KEY_B64": pub, "BUNDLE_PUBLIC_KEY_B64": pub}):
        same = next(c for c in dp.run_preflight()["checks"] if c["id"] == "release_key_distinct")
        assert same["status"] == "fail" and "IDENTICAL" in same["current"]
    with patch.dict(os.environ, {**base, "RELEASE_PUBLIC_KEY_B64": pub, "BUNDLE_PUBLIC_KEY_B64": pub2}):
        assert next(c for c in dp.run_preflight()["checks"] if c["id"] == "release_key_distinct")["status"] == "pass"


def test_n102_5_ea_record_signature_covers_the_key_id():
    import ea_capabilities as ec
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import verify_ea_release as v
    rec = {"version": "1.60", "mq5_sha256": "a" * 64, "ex5_sha256": "b" * 64, "metaeditor_version": "5.00 build 4620",
           "windows_build": "x", "mt5_build": "4620", "source_commit": "c" * 40}
    p_release = v._canonical_payload(rec, "stoic-release-ed25519-v1")
    assert b'"key_id":"stoic-release-ed25519-v1"' in p_release
    assert p_release != v._canonical_payload(rec, "stoic-bundle-ed25519-v1")
    signed = {**rec, "signature": {"key_id": "stoic-release-ed25519-v1", "sig_hex": "00"}}
    assert ec._canonical_payload(signed) == p_release == v._canonical_payload(signed)
    relabelled = {**rec, "signature": {"key_id": "stoic-bundle-ed25519-v1", "sig_hex": "00"}}
    assert ec._canonical_payload(relabelled) != p_release
    assert "_kid(purpose=\"ea-release\")" in _read("scripts/verify_ea_release.py")


def test_gitleaks_false_positive_is_ignored_and_fixture_no_longer_matches():
    gi = _read(".gitleaksignore")
    assert "333722e835ff6c9f6897de660009cf55f975c399:backend/tests/unit/test_fixplan_main101.py:generic-api-key:203" in gi
    t = _read("backend/tests/unit/test_fixplan_main101.py")
    assert 'sidecar_env["ED25519_SIGNING_KEY" + "_B64"] = priv' in t
