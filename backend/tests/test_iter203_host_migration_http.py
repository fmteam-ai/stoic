"""iter-203 · Admin Host Migration wizard — API proxy contract (live target).

Works in both preview states: sidecar enabled (MIGRATOR_URL set → real state
payload) or not (503 migrator_not_enabled). Never advances a real migration:
only status/public-key/events/validation paths are exercised.
"""
import uuid

import pytest
import requests

from live_target import require_live_base_url, resolve_admin_credentials

BASE_URL = require_live_base_url()
API = f"{BASE_URL}/api"


def _login(email, pw):
    s = requests.Session()
    r = s.post(f"{API}/auth/login", json={"email": email, "password": pw}, timeout=15)
    assert r.status_code == 200, r.text
    csrf = s.cookies.get("csrf_token")
    if csrf:
        s.headers.update({"X-CSRF-Token": csrf})
    return s


@pytest.fixture(scope="module")
def admin():
    return _login(*resolve_admin_credentials())


def test_status_contract(admin):
    r = admin.get(f"{API}/admin/host-migration/status", timeout=30)
    assert r.status_code in (200, 503), r.text
    if r.status_code == 503:
        assert r.json()["detail"]["code"] == "migrator_not_enabled"
        return
    st = r.json()["state"]
    assert st["status"] in ("idle", "awaiting", "running", "failed", "done", "aborted")
    assert [s["id"] for s in st["steps"]] == ["preflight", "install", "warm_sync", "freeze", "final_sync",
                                              "verify_target", "cutover_check", "decommission"]
    assert isinstance(st["log"], list) and "simulated" in st


def test_public_key_when_enabled(admin):
    r = admin.get(f"{API}/admin/host-migration/public-key", timeout=30)
    assert r.status_code in (200, 503)
    if r.status_code == 200:
        assert r.json()["public_key"].startswith("ssh-ed25519")


def test_input_validation_before_any_sidecar_call(admin):
    r = admin.post(f"{API}/admin/host-migration/preflight",
                   json={"host": "evil; rm -rf /", "user": "stoic", "path": "/home/stoic/stoic"}, timeout=15)
    assert r.status_code == 422
    r = admin.post(f"{API}/admin/host-migration/preflight",
                   json={"host": "203.0.113.10", "user": "Root;x", "path": "/home/stoic/stoic"}, timeout=15)
    assert r.status_code == 422
    r = admin.post(f"{API}/admin/host-migration/advance", json={"step": "wipe"}, timeout=15)
    assert r.status_code == 422


def test_destructive_steps_need_step_up(admin):
    # freeze/decommission/abort/start require a fresh 2FA step-up token BEFORE the sidecar is contacted
    s = requests.Session()
    s.cookies.update(admin.cookies)
    s.headers.update({k: v for k, v in admin.headers.items() if k != "X-Step-Up-Bypass"})
    s.headers["X-Step-Up-Bypass"] = ""          # disable the conftest test bypass → real gate
    for path, body in (("start", {}), ("advance", {"step": "freeze"}), ("advance", {"step": "decommission"}), ("abort", {})):
        r = s.post(f"{API}/admin/host-migration/{path}", json=body, timeout=15)
        assert r.status_code == 403, f"{path}: {r.status_code} {r.text}"
        assert r.json()["detail"]["code"] in ("step_up_required", "mfa_enrollment_required"), r.text


def test_events_journal(admin):
    r = admin.get(f"{API}/admin/host-migration/events", timeout=15)
    assert r.status_code == 200 and isinstance(r.json()["events"], list)


def test_non_admin_forbidden():
    email = f"hm_{uuid.uuid4().hex[:8]}@example.com"
    pw = "HostMig#2026!"
    r = requests.post(f"{API}/auth/register", json={"email": email, "password": pw, "name": "H", "terms_agreed": True}, timeout=15)
    assert r.status_code in (200, 201), r.text
    from conftest import run_async
    from database import get_db
    run_async(get_db().users.update_one({"email": email}, {"$set": {"email_verified": True}}))
    s = _login(email, pw)
    for path in ("status", "public-key", "events"):
        assert s.get(f"{API}/admin/host-migration/{path}", timeout=15).status_code == 403, path
    assert s.post(f"{API}/admin/host-migration/start", json={}, timeout=15).status_code == 403
    run_async(get_db().users.delete_one({"email": email}))
