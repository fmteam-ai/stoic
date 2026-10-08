"""v1.60.5 — the install-from-archive backend never became healthy: compose `env_file` + python-dotenv
turn every untouched template line `KEY=` into KEY="" and `int(os.environ.get("KEY", "60"))` dies at
import. Blank env values must be treated as UNSET before any module parses them."""
import os
import re
import subprocess
import sys

import pytest

import secrets_loader

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
BACKEND = os.path.join(ROOT, "backend")

pytestmark = pytest.mark.unit


def _template_keys():
    keys = []
    for line in open(os.path.join(ROOT, "deploy", "env", "backend.env.example")).read().splitlines():
        m = re.match(r"^([A-Z][A-Z0-9_]*)=$", line.strip())
        if m:
            keys.append(m.group(1))
    assert len(keys) > 40, "template lost its blank keys?"
    return keys


def test_drop_blank_env_removes_every_blank_template_key(monkeypatch):
    for k in _template_keys():
        monkeypatch.setenv(k, "")
    monkeypatch.setenv("STOIC_KEEP_ME", "value")
    n = secrets_loader.drop_blank_env()
    assert n >= len(_template_keys())
    assert all(k not in os.environ for k in _template_keys())
    assert os.environ["STOIC_KEEP_ME"] == "value"


def test_resolve_file_secrets_fills_blank_target_from_file(monkeypatch, tmp_path):
    p = tmp_path / "jwt"
    p.write_text("from-file\n")
    monkeypatch.setenv("STOIC_T_JWT", "")
    monkeypatch.setenv("STOIC_T_JWT_FILE", str(p))
    monkeypatch.setenv("STOIC_T_INT", "")
    secrets_loader.resolve_file_secrets()
    assert os.environ["STOIC_T_JWT"] == "from-file"
    assert "STOIC_T_INT" not in os.environ and int(os.environ.get("STOIC_T_INT", "60")) == 60


def test_entrypoints_resolve_secrets_before_vault_overlay():
    # the vault overlay needs MONGO_URL — in Docker it only exists after *_FILE resolution
    for rel in ("server.py", os.path.join("workers", "base.py")):
        src = open(os.path.join(BACKEND, rel)).read()
        assert src.index("resolve_file_secrets()") < src.index("_load_vault("), rel


def test_security_module_imports_with_blank_template_env():
    """Regression for the exact CI crash: security.py line REFRESH_REUSE_GRACE_SECONDS = int(...)."""
    env = {k: "" for k in _template_keys()}
    env.update({"PATH": os.environ["PATH"], "PYTHONPATH": BACKEND, "MONGO_URL": "mongodb://localhost:27017",
                "DB_NAME": "stoic_unit", "JWT_SECRET": "x" * 32})
    code = ("from secrets_loader import resolve_file_secrets; resolve_file_secrets(); "
            "import security; print(security.REFRESH_REUSE_GRACE_SECONDS)")
    r = subprocess.run([sys.executable, "-c", code], env=env, cwd=BACKEND, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-1500:]
    assert r.stdout.strip() == "60"


def test_installer_diagnostics_and_healthcheck_start_period():
    lib = open(os.path.join(ROOT, "deploy", "lib.sh")).read()
    assert "docker compose ps -aq" in lib and "compose ps -a --format" not in lib
    assert "docker compose logs --tail 40 backend" in lib
    df = open(os.path.join(ROOT, "Dockerfile.backend")).read()
    assert "--start-period=" in df
    wf = open(os.path.join(ROOT, ".github", "workflows", "release.yml")).read()
    assert "Container logs on failure" in wf
