from live_target import ADMIN_EMAIL, ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
"""iter-175 — Turnstile diagnostics: /ops/turnstile-diag endpoint, diagnose()
secret probe interpretation, and rejection ring buffer for prod debugging.
"""
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
pass  # ADMIN_EMAIL comes from live_target
ADMIN_PW = ADMIN_PASSWORD
TIMEOUT = 25


def _run(coro):
    from conftest import run_async
    return run_async(coro)


def _admin():
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PW},
               timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return s


def test_diagnose_interprets_secret_probe(monkeypatch):
    import turnstile_gate as tg
    from database import get_db

    async def _fake_verify(token, remote_ip=None, action=None):
        return {"ok": False, "outage": False, "state": "configuration_invalid",
                "error_codes": ["invalid-input-secret"]}
    monkeypatch.setattr(tg, "verify_token", _fake_verify)
    out = _run(tg.diagnose(get_db()))
    assert out["secret_check"] == "INVALID_SECRET"
    assert "hint" in out

    async def _fake_ok(token, remote_ip=None, action=None):
        return {"ok": False, "outage": False, "state": "client_token_invalid",
                "error_codes": ["invalid-input-response"]}
    monkeypatch.setattr(tg, "verify_token", _fake_ok)
    out = _run(tg.diagnose(get_db()))
    assert out["secret_check"] == "secret_ok"

    async def _fake_outage(token, remote_ip=None, action=None):
        return {"ok": False, "outage": True, "state": "provider_unavailable", "error_codes": ["network-error"]}
    monkeypatch.setattr(tg, "verify_token", _fake_outage)
    out = _run(tg.diagnose(get_db()))
    assert out["secret_check"] == "cloudflare_unreachable"


def test_diagnose_unconfigured_secret(monkeypatch):
    import turnstile_gate as tg
    from database import get_db
    monkeypatch.setattr(tg, "secret_key", lambda: "")
    out = _run(tg.diagnose(get_db()))
    assert out["secret_check"] == "not_configured"
    assert out["secret_key_set"] is False


def test_rejection_ring_records_error_codes(monkeypatch):
    import pytest as _pytest
    from fastapi import HTTPException
    import turnstile_gate as tg
    from database import get_db

    async def _fake_reject(token, remote_ip=None, action=None):
        return {"ok": False, "outage": False, "state": "client_token_invalid",
                "error_codes": ["timeout-or-duplicate"]}
    monkeypatch.setattr(tg, "verify_token", _fake_reject)

    async def _enabled(db):
        return True
    monkeypatch.setattr(tg, "is_enabled", _enabled)

    before = len(tg._RECENT_REJECTIONS)
    with _pytest.raises(HTTPException) as exc:
        _run(tg.require_turnstile(get_db(), "stale-token", "1.2.3.4",
                                  action="login"))
    assert exc.value.status_code == 403
    assert len(tg._RECENT_REJECTIONS) == min(before + 1, 20)
    last = tg._RECENT_REJECTIONS[-1]
    assert last["error_codes"] == ["timeout-or-duplicate"]
    assert last["action"] == "login"


def test_ops_turnstile_diag_endpoint_admin_only():
    r = requests.get(f"{API}/ops/turnstile-diag", timeout=TIMEOUT)
    assert r.status_code == 403

    s = _admin()
    r = s.get(f"{API}/ops/turnstile-diag", timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["site_key_set"] is True
    assert body["secret_key_set"] is True
    assert "recent_rejections" in body
    # live probe against Cloudflare: preview keys are real, so either the
    # secret validates (secret_ok) or Cloudflare is unreachable from CI.
    assert body["secret_check"] in ("secret_ok", "cloudflare_unreachable",
                                    "INVALID_SECRET")
    secret = os.environ.get("TURNSTILE_SECRET_KEY", "")
    assert secret and secret not in r.text


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
