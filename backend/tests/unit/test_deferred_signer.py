"""Publish unblock — RELEASE_SIGNER deferred mode + managed-platform provenance."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

PROD_SECRETS = {"APP_ENV": "production", "RELEASE_SIGNER": "local", "RELEASE_SIGNER_ALLOW_LOCAL_IN_PROD": "true",
                "RELEASE_SIGNER_DEFERRED": "true", "ED25519_SIGNING_KEY_B64": "QUFB" * 11,
                "PRODUCTION_RETIRED_SECRETS": "ED25519_SIGNING_KEY_B64,STEP_UP_BYPASS_TOKEN,RATE_LIMIT_BYPASS_TOKEN"}


def test_deferred_flag_overrides_uneditable_local_mode_in_production_only():
    from release_signing import _mode, signer_config_violations
    assert _mode(PROD_SECRETS) == "deferred"
    assert signer_config_violations(PROD_SECRETS) == []                       # boots
    preview = {**PROD_SECRETS, "APP_ENV": "preview"}
    assert _mode(preview) == "local"                                          # preview keeps its test signer
    strict = {k: v for k, v in PROD_SECRETS.items() if k != "RELEASE_SIGNER_DEFERRED"}
    assert any("forbids RELEASE_SIGNER=local" in v for v in signer_config_violations(strict))


def test_deferred_with_live_private_key_is_still_refused():
    from release_signing import signer_config_violations
    env = {k: v for k, v in PROD_SECRETS.items() if k != "PRODUCTION_RETIRED_SECRETS"}
    assert any("forbids ED25519_SIGNING_KEY_B64" in v for v in signer_config_violations(env))


def test_deferred_never_signs_and_gates_live_exposure(monkeypatch):
    for k, v in PROD_SECRETS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("RELEASE_PUBLIC_KEY_B64", raising=False)
    import release_signing as rs
    with pytest.raises(rs.SignerDeferred):
        rs.sign_hex(b"anything", purpose="ea-release")
    with pytest.raises(rs.SignerDeferred):
        rs.public_key_b64()
    assert rs.deferred_gate()["code"] == "RELEASE_SIGNER_DEFERRED"
    assert rs.signer_status()["deferred"] is True
    h = rs.signer_health()
    assert h["ok"] is False and h.get("deferred") is True
    monkeypatch.setenv("RELEASE_SIGNER_DEFERRED", "false")
    assert rs.deferred_gate() is None


def test_authority_and_activation_consult_deferred_gate():
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
    assert "deferred_gate() or live_gate(account)" in open(os.path.join(root, "trading_authority.py")).read()
    assert "deferred_gate() or live_gate(account)" in open(os.path.join(root, "routes", "bot_routes.py")).read()
    assert "from modules.pamm import strategy_guard as _sg" in open(os.path.join(root, "server.py")).read()


def test_source_tree_digest_is_stable_and_content_bound(tmp_path):
    from modules.pamm.strategy_guard import source_tree_digest
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "t.py").write_text("ignored\n")
    d1 = source_tree_digest(tmp_path)
    assert d1.startswith("source-sha256:") and d1 == source_tree_digest(tmp_path)
    (tmp_path / "tests" / "t.py").write_text("changed but ignored\n")
    assert source_tree_digest(tmp_path) == d1
    (tmp_path / "a.py").write_text("x = 2\n")
    assert source_tree_digest(tmp_path) != d1
