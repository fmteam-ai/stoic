"""deploy/env/*.env.example is the committed source of truth for the env templates
(the platform auto-commit skips every .env* path, so CI must materialise them)."""
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))

pytestmark = pytest.mark.unit


def _src(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def test_dot_env_examples_match_their_committed_sources():
    import sync_env_examples as sync
    assert sync.main(["--check"]) == 0, "run: python scripts/sync_env_examples.py"


def test_committed_templates_carry_the_keys_ci_asserts_on():
    be = _src("deploy", "env", "backend.env.example")
    for k in ("BRIDGE_TOKEN_HASH_KEY=", "CRYPTO_LIVE_TRADING_ENABLED=", "LEDGER_ANCHOR_KEY=", "JWT_SECRET=", "MONGO_URL="):
        assert k in be, k
    root = _src("deploy", "env", "root.env.example")
    for k in ("BRIDGE_TOKEN_HASH_KEY=", "CRYPTO_LIVE_TRADING_ENABLED=", "LEDGER_ANCHOR_KEY=", "DB_NAME="):
        assert k in root, k
    # templates never carry values
    for body in (be, root):
        for line in body.splitlines():
            if line and not line.startswith("#") and "=" in line:
                assert line.endswith("="), f"value in template: {line}"


def test_ci_release_and_installer_materialise_templates_before_use():
    ci = _src(".github", "workflows", "ci.yml")
    assert ci.count("scripts/sync_env_examples.py") >= 4          # unit, integration, clean-deploy, ea-structural
    assert ci.index("sync_env_examples.py") < ci.index("python -m pytest tests/unit")
    rel = _src(".github", "workflows", "release.yml")
    assert "cd /tmp/release && python3 scripts/sync_env_examples.py" in rel
    assert rel.index("sync_env_examples.py") < rel.index("python -m pytest tests/unit")
    inst = _src("deploy", "install.sh")
    assert "deploy/env/backend.env.example backend/.env.example" in inst
    assert inst.index("sync_env_templates()") < inst.index("cp .env.example .env")


def test_ea_release_fails_clearly_when_signer_secrets_are_missing():
    wf = _src(".github", "workflows", "ea-release.yml")
    assert "Signer secrets present" in wf and "docs/RELEASE_SIGNER.md" in wf
    assert wf.index("Signer secrets present") < wf.index("Record + sign the EX5")
    assert "GitHub Actions secrets" in _src("docs", "RELEASE_SIGNER.md")
