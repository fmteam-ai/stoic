"""iter-181 — Deploy preflight: production guardrail simulator."""
import os
import sys

import requests

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)
from dotenv import load_dotenv
load_dotenv(os.path.join(_BACKEND_DIR, ".env"))

from live_target import require_live_base_url
BASE_URL = require_live_base_url()
API = f"{BASE_URL}/api"
ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PW = "admin123"
TIMEOUT = 25

GOOD_ENV = {
    "APP_ENV": "production",
    "CSRF_ENFORCE_ORIGIN": "true",
    "CORS_ORIGINS": "https://stoicaibot.com,https://www.stoicaibot.com",
    "STEP_UP_BYPASS_TOKEN": "",
    "RATE_LIMIT_BYPASS_TOKEN": "",
    "ADMIN_MFA_ENFORCED": "true",
    "ADMIN_PASSWORD": "TestDummy-Pass-2026-NotReal",  # dummy, not a credential
    "ED25519_SIGNING_KEY_B64": "testkey-testkey-testkey",  # dummy, not a key
    "KEY_VAULT_MASTER": "vault-master-material",
    "RELEASE_SIGNER": "external",
    "RELEASE_SIGNER_ALLOW_LOCAL_IN_PROD": None,
    "BACKGROUND_WORKERS_IN_PROCESS": "true",
}


def _apply(monkeypatch, overrides):
    for k, v in {**GOOD_ENV, **overrides}.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)


def _by_id(out):
    return {c["id"]: c for c in out["checks"]}


def test_preflight_all_good_is_ready(monkeypatch):
    from deploy_preflight import run_preflight
    _apply(monkeypatch, {})
    out = run_preflight()
    assert out["verdict"] in ("ready", "ready_with_warnings")
    assert out["fail_count"] == 0
    # external signer is the only passing configuration (v56)
    assert _by_id(out)["release_signer"]["status"] == "pass"


def test_preflight_flags_each_boot_blocker(monkeypatch):
    from deploy_preflight import run_preflight
    cases = {
        "cors": {"CORS_ORIGINS": ""},
        "step_up_bypass_token": {"STEP_UP_BYPASS_TOKEN": "leaked"},
        "rate_limit_bypass_token": {"RATE_LIMIT_BYPASS_TOKEN": "leaked"},
        "admin_mfa": {"ADMIN_MFA_ENFORCED": "false"},
        "admin_password": {"ADMIN_PASSWORD": "admin123"},
        "ed25519": {"ED25519_SIGNING_KEY_B64": None},
        "key_vault": {"KEY_VAULT_MASTER": None},
    }
    for cid, override in cases.items():
        _apply(monkeypatch, override)
        out = run_preflight()
        assert out["verdict"] == "will_crash", cid
        assert _by_id(out)[cid]["status"] == "fail", cid


def test_preflight_csrf_and_cors_need_no_secret(monkeypatch):
    """iter-182: CSRF is auto-enforced in prod; CORS dev entries auto-filter."""
    from deploy_preflight import run_preflight
    _apply(monkeypatch, {
        "CSRF_ENFORCE_ORIGIN": None,
        "CORS_ORIGINS": "http://localhost:3000,"
                        "https://x.preview.emergentagent.com,"
                        "https://stoicaibot.com"})
    out = run_preflight()
    assert _by_id(out)["csrf"]["status"] == "pass"
    assert _by_id(out)["cors"]["status"] == "pass"
    assert "stoicaibot.com" in _by_id(out)["cors"]["current"]
    assert "localhost" not in _by_id(out)["cors"]["current"].split(" (")[0]
    # localhost/preview-only list has no real prod origin → boot refused
    _apply(monkeypatch, {"CORS_ORIGINS": "http://localhost:3000"})
    out = run_preflight()
    assert _by_id(out)["cors"]["status"] == "fail"
    assert out["verdict"] == "will_crash"


def test_preflight_local_signer_fails_in_production(monkeypatch):
    """v56 / review P1-9: local signing in production is a FAIL (KMS/HSM
    required); the old acknowledgment escape hatch is a misconfiguration."""
    from deploy_preflight import run_preflight
    _apply(monkeypatch, {"RELEASE_SIGNER": "local",
                         "RELEASE_SIGNER_ALLOW_LOCAL_IN_PROD": None})
    out = run_preflight()
    assert _by_id(out)["release_signer"]["status"] == "fail"
    assert "KMS/HSM" in _by_id(out)["release_signer"]["current"]
    _apply(monkeypatch, {"RELEASE_SIGNER": "local",
                         "RELEASE_SIGNER_ALLOW_LOCAL_IN_PROD": "true"})
    assert _by_id(run_preflight())["release_signer"]["status"] == "fail"


def test_csrf_origin_auto_enforced_in_production(monkeypatch):
    from security import _allowed_origins, csrf_origin_enforced
    monkeypatch.delenv("CSRF_ENFORCE_ORIGIN", raising=False)
    monkeypatch.setenv("APP_ENV", "preview")
    assert csrf_origin_enforced() is False
    monkeypatch.setenv("APP_ENV", "production")
    assert csrf_origin_enforced() is True
    monkeypatch.setenv(
        "CORS_ORIGINS",
        "http://localhost:3000,https://a.preview.emergentagent.com,"
        "https://stoicaibot.com")
    assert _allowed_origins() == {"https://stoicaibot.com"}
    monkeypatch.setenv("APP_ENV", "preview")
    assert len(_allowed_origins()) == 3


def test_preflight_short_admin_password_fails(monkeypatch):
    from deploy_preflight import run_preflight
    _apply(monkeypatch, {"ADMIN_PASSWORD": "Short1!"})
    out = run_preflight()
    assert _by_id(out)["admin_password"]["status"] == "fail"
    assert "too short" in _by_id(out)["admin_password"]["current"]


def test_preflight_no_secret_values_leaked(monkeypatch):
    from deploy_preflight import run_preflight
    _apply(monkeypatch, {})
    import json
    blob = json.dumps(run_preflight())
    assert GOOD_ENV["ADMIN_PASSWORD"] not in blob
    assert GOOD_ENV["KEY_VAULT_MASTER"] not in blob
    assert GOOD_ENV["ED25519_SIGNING_KEY_B64"] not in blob


def test_preflight_endpoint_admin_only():
    r = requests.get(f"{API}/ops/deploy-preflight", timeout=TIMEOUT)
    assert r.status_code == 403
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PW},
               timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    r = s.get(f"{API}/ops/deploy-preflight", timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["environment"] == "preview"
    assert {"verdict", "checks", "note"} <= set(body)
    ids = {c["id"] for c in body["checks"]}
    assert {"csrf", "cors", "admin_password", "ed25519",
            "release_signer"} <= ids


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
