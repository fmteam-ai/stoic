"""iter-223 · Admin Host Migration wizard — FULL REHEARSAL flow.

Extends iter-203 (contract/gate) with the end-to-end simulated migration
against the loopback sidecar in MIGRATOR_DRY_RUN=1 mode. Uses the pytest
`X-Step-Up-Bypass` header injected by conftest to walk every gate.
"""
import os
import time
import uuid

import pytest
import requests

from live_target import require_live_base_url, resolve_admin_credentials

BASE_URL = require_live_base_url()
API = f"{BASE_URL}/api"
HM = f"{API}/admin/host-migration"


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
    s = _login(*resolve_admin_credentials())
    r = s.get(f"{HM}/status", timeout=15)
    if r.status_code in (502, 503):
        pytest.skip("host-migration sidecar not running on this target (MIGRATOR_URL unset or "
                    "rehearsal process down) — see /app/memory/PRD.md iter-203 to restart it")
    return s


def _status(sess):
    r = sess.get(f"{HM}/status", timeout=15)
    assert r.status_code == 200, r.text
    return r.json()["state"]


def _reset_if_needed(sess):
    """Ensure sidecar is in a state where a fresh preflight is accepted."""
    st = _status(sess)
    if st["status"] == "running":
        # wait for it to settle up to 30s
        for _ in range(30):
            time.sleep(1)
            st = _status(sess)
            if st["status"] != "running":
                break
    if st["status"] == "awaiting" and st.get("current_step") not in (None, "preflight"):
        r = sess.post(f"{HM}/abort", timeout=30)
        assert r.status_code in (200, 202), r.text


def _poll_until(sess, predicate, timeout=45, interval=1.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = _status(sess)
        if predicate(last):
            return last
        time.sleep(interval)
    raise AssertionError(f"timeout waiting for predicate; last state: "
                         f"status={last and last.get('status')} awaiting={last and last.get('awaiting')} "
                         f"current={last and last.get('current_step')}")


def test_public_key_starts_with_ssh_ed25519(admin):
    r = admin.get(f"{HM}/public-key", timeout=15)
    assert r.status_code == 200, r.text
    assert r.json()["public_key"].startswith("ssh-ed25519")


def test_preflight_success_and_awaiting_install(admin):
    _reset_if_needed(admin)
    body = {"host": "203.0.113.10", "user": "stoic", "port": 22, "path": "/home/stoic/stoic"}
    r = admin.post(f"{HM}/preflight", json=body, timeout=60)
    assert r.status_code == 200, r.text
    facts = r.json()
    assert facts["ok"] is True
    checks = facts["target"]["checks"]
    assert all(checks.values()), f"failing checks: {[k for k,v in checks.items() if not v]}"
    src = facts["source"]
    assert src.get("tag") == "v0.0.0-rehearsal"
    assert facts["host_key"]["fingerprints"], "expected host_key fingerprints"

    st = _status(admin)
    assert st["status"] == "awaiting"
    assert st["awaiting"] == "install"
    assert st["simulated"] is True
    # 8 canonical steps
    assert [s["id"] for s in st["steps"]] == [
        "preflight", "install", "warm_sync", "freeze", "final_sync",
        "verify_target", "cutover_check", "decommission"]


def test_advance_freeze_before_start_conflict(admin):
    # State is awaiting install now; advancing freeze must not be accepted
    r = admin.post(f"{HM}/advance", json={"step": "freeze"}, timeout=15)
    # backend returns 409 from sidecar
    assert r.status_code == 409, r.text


def test_full_rehearsal_flow(admin):
    # Ensure awaiting=install (from preflight test above)
    st = _status(admin)
    if not (st["status"] == "awaiting" and st["awaiting"] == "install"):
        _reset_if_needed(admin)
        body = {"host": "203.0.113.10", "user": "stoic", "port": 22, "path": "/home/stoic/stoic"}
        r = admin.post(f"{HM}/preflight", json=body, timeout=60)
        assert r.status_code == 200

    # start (install + warm_sync)
    r = admin.post(f"{HM}/start", json={}, timeout=15)
    assert r.status_code in (200, 202), r.text
    st = _poll_until(admin, lambda s: s["status"] == "awaiting" and s["awaiting"] == "freeze", timeout=45)
    assert next(x for x in st["steps"] if x["id"] == "install")["status"] == "done"
    assert next(x for x in st["steps"] if x["id"] == "warm_sync")["status"] == "done"

    # advance freeze (chain: freeze → final_sync → verify_target)
    r = admin.post(f"{HM}/advance", json={"step": "freeze"}, timeout=15)
    assert r.status_code in (200, 202), r.text
    st = _poll_until(admin, lambda s: s["status"] == "awaiting" and s["awaiting"] == "cutover_check", timeout=60)
    assert st.get("downtime_started_at"), "downtime_started_at must be set after freeze"
    vt = next(x for x in st["steps"] if x["id"] == "verify_target")
    assert vt["status"] == "done"
    readiness = vt.get("detail", {}).get("readiness")
    assert isinstance(readiness, dict) and readiness.get("ready") is True

    # decommission is not permitted before cutover ok
    r = admin.post(f"{HM}/advance", json={"step": "decommission"}, timeout=15)
    assert r.status_code == 409, r.text

    # cutover_check may need to be retried until simulated EA heartbeats appear
    # (simulated_run returns 2 heartbeats only when time.time()%60 > 20).
    ok = False
    for _ in range(70):
        r = admin.post(f"{HM}/advance", json={"step": "cutover_check"}, timeout=30)
        # accept both 200/202 or 409 when a step is still running
        if r.status_code in (200, 202):
            # wait for the cutover_check step to finish
            _poll_until(admin, lambda s: s["status"] != "running", timeout=15)
        elif r.status_code != 409:
            pytest.fail(f"unexpected cutover_check response {r.status_code}: {r.text}")
        st = _status(admin)
        cut = (st.get("facts") or {}).get("cutover") or {}
        if st["awaiting"] == "decommission" or cut.get("ok"):
            ok = True
            break
        time.sleep(2)
    assert ok, f"cutover.ok never became true; last state awaiting={st.get('awaiting')}, cutover={cut}"

    # decommission
    r = admin.post(f"{HM}/advance", json={"step": "decommission"}, timeout=15)
    assert r.status_code in (200, 202), r.text
    st = _poll_until(admin, lambda s: s["status"] == "done", timeout=30)
    assert all(x["status"] == "done" for x in st["steps"]), \
        f"not all steps done: {[(x['id'], x['status']) for x in st['steps']]}"


def test_new_preflight_after_done_resets_state(admin):
    prev = _status(admin)
    assert prev["status"] == "done"
    prev_id = prev["id"]
    body = {"host": "203.0.113.10", "user": "stoic", "port": 22, "path": "/home/stoic/stoic"}
    r = admin.post(f"{HM}/preflight", json=body, timeout=60)
    assert r.status_code == 200
    st = _status(admin)
    assert st["id"] != prev_id
    assert st["awaiting"] == "install"


def test_abort_while_awaiting(admin):
    st = _status(admin)
    assert st["status"] == "awaiting"
    r = admin.post(f"{HM}/abort", timeout=30)
    assert r.status_code == 200, r.text
    st = _status(admin)
    assert st["status"] == "aborted"


def test_events_journal_has_actions(admin):
    r = admin.get(f"{HM}/events?limit=100", timeout=15)
    assert r.status_code == 200
    events = r.json()["events"]
    actions = {e["action"] for e in events}
    # Should contain at least preflight, start, and some advance:* + abort
    assert "preflight" in actions
    assert "start" in actions
    assert any(a.startswith("advance:") for a in actions), f"actions: {actions}"
    assert "abort" in actions
    # each row has actor / user_id / detail
    row = events[0]
    for k in ("at", "actor", "user_id", "action", "detail"):
        assert k in row


def test_real_step_up_gate_on_destructive_actions(admin):
    """Without the bypass header, start/advance freeze/advance decommission/abort must be 403."""
    s = requests.Session()
    s.cookies.update(admin.cookies)
    s.headers.update({k: v for k, v in admin.headers.items() if k != "X-Step-Up-Bypass"})
    s.headers["X-Step-Up-Bypass"] = ""  # disable conftest bypass
    for path, body in (("start", {}),
                       ("advance", {"step": "freeze"}),
                       ("advance", {"step": "decommission"}),
                       ("abort", {})):
        r = s.post(f"{HM}/{path}", json=body, timeout=15)
        assert r.status_code == 403, f"{path}: {r.status_code} {r.text}"
        code = r.json()["detail"]["code"]
        assert code in ("step_up_required", "mfa_enrollment_required"), r.text


def test_cutover_check_and_retry_no_step_up(admin):
    """advance{cutover_check} and retry do NOT require step-up."""
    s = requests.Session()
    s.cookies.update(admin.cookies)
    s.headers.update({k: v for k, v in admin.headers.items() if k != "X-Step-Up-Bypass"})
    s.headers["X-Step-Up-Bypass"] = ""
    # cutover_check: no step-up gate → should NOT be 403 (state may cause 409/500 but not 403)
    r = s.post(f"{HM}/advance", json={"step": "cutover_check"}, timeout=15)
    assert r.status_code != 403, r.text
    r = s.post(f"{HM}/retry", timeout=15)
    assert r.status_code != 403, r.text


def test_advance_unknown_step_rejected(admin):
    r = admin.post(f"{HM}/advance", json={"step": "wipe"}, timeout=15)
    assert r.status_code == 422


def test_preflight_input_validation(admin):
    for payload in (
        {"host": "evil; rm -rf /", "user": "stoic", "path": "/home/stoic/stoic"},
        {"host": "203.0.113.10", "user": "Root;x", "path": "/home/stoic/stoic"},
        {"host": "203.0.113.10", "user": "stoic", "path": "../evil"},
    ):
        r = admin.post(f"{HM}/preflight", json=payload, timeout=15)
        assert r.status_code == 422, f"{payload} → {r.status_code} {r.text}"


def test_non_admin_forbidden_on_all_endpoints():
    email = f"hm_{uuid.uuid4().hex[:8]}@example.com"
    pw = "HostMig#2026!"
    r = requests.post(f"{API}/auth/register",
                      json={"email": email, "password": pw, "name": "H", "terms_agreed": True}, timeout=15)
    assert r.status_code in (200, 201), r.text
    from conftest import run_async
    from database import get_db
    run_async(get_db().users.update_one({"email": email}, {"$set": {"email_verified": True}}))
    s = _login(email, pw)
    for path in ("status", "public-key", "events"):
        assert s.get(f"{HM}/{path}", timeout=15).status_code == 403, path
    for path, body in (("preflight", {"host": "203.0.113.10", "user": "stoic", "path": "/home/stoic/stoic"}),
                       ("start", {}), ("advance", {"step": "cutover_check"}),
                       ("retry", {}), ("abort", {})):
        assert s.post(f"{HM}/{path}", json=body, timeout=15).status_code == 403, path
    run_async(get_db().users.delete_one({"email": email}))
