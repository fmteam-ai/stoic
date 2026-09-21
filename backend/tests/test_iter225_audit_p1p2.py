"""iter-225 · Audit round 4 P1/P2 corrections.

P1-1: two-phase host-key trust — scan (no login) then admin picks +
      re-types exact fingerprint → TRUST KEY (2FA) + preflight. Includes
      fingerprint format validation and journal events.
P1-2: cutover unlocks decommission only when EVERY expected enabled
      account identity reconnected. Audited DISABLE MISSING exception.
P1-5: dedicated migration token (not METRICS_TOKEN).
P2-2: public trust section withheld until TRUST_STATS_LEGAL_APPROVED=true.

Extends existing suites — does NOT duplicate iter-203/iter-223.
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
SIM_FP = "SHA256:Simulated0Fingerprint0Rehearsal0Mode0000000"
BAD_FP_FORMAT = "not-a-fingerprint"
WRONG_FP = "SHA256:" + "A" * 43  # valid FORMAT, non-matching


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
        pytest.skip("host-migration sidecar not running")
    return s


def _status(sess):
    r = sess.get(f"{HM}/status", timeout=15)
    assert r.status_code == 200, r.text
    return r.json()["state"]


def _reset(sess):
    st = _status(sess)
    if st["status"] == "running":
        for _ in range(30):
            time.sleep(1)
            st = _status(sess)
            if st["status"] != "running":
                break
    if st["status"] in ("awaiting", "failed") and st.get("current_step") not in (None, "preflight"):
        sess.post(f"{HM}/abort", timeout=30)


def _poll_until(sess, predicate, timeout=60, interval=1.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = _status(sess)
        if predicate(last):
            return last
        time.sleep(interval)
    raise AssertionError(f"timeout; last: status={last and last.get('status')} awaiting={last and last.get('awaiting')}")


# ─── P1-1 · SCAN (Phase 1 · no login, no trust) ─────────────────────────────
def test_scan_happy_path_and_journals(admin):
    r = admin.post(f"{HM}/scan", json={"host": "203.0.113.10"}, timeout=60)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["host"] == "203.0.113.10"
    assert data["port"] == 22
    assert isinstance(data["fingerprints"], list) and len(data["fingerprints"]) >= 1
    for fp in data["fingerprints"]:
        assert "fingerprint" in fp and "type" in fp
        assert fp["fingerprint"].startswith("SHA256:")
    assert data["keys"] >= 1
    assert "scanned_at" in data

    # journal event 'host_key_observed'
    ev = admin.get(f"{HM}/events?limit=50", timeout=15).json()["events"]
    assert any(e["action"] == "host_key_observed" for e in ev), \
        f"missing host_key_observed in {[e['action'] for e in ev[:10]]}"


def test_scan_invalid_host_422(admin):
    r = admin.post(f"{HM}/scan", json={"host": "evil; rm -rf /"}, timeout=15)
    assert r.status_code == 422, r.text


# ─── P1-1 · PREFLIGHT fingerprint validation ────────────────────────────────
def test_preflight_missing_fingerprint_422(admin):
    body = {"host": "203.0.113.10", "user": "stoic", "port": 22, "path": "/home/stoic/stoic"}
    r = admin.post(f"{HM}/preflight", json=body, timeout=15)
    assert r.status_code == 422, r.text


def test_preflight_bad_format_fingerprint_422(admin):
    body = {"host": "203.0.113.10", "user": "stoic", "port": 22, "path": "/home/stoic/stoic",
            "accept_fingerprint": BAD_FP_FORMAT}
    r = admin.post(f"{HM}/preflight", json=body, timeout=15)
    assert r.status_code == 422, r.text


def test_preflight_wrong_but_valid_fingerprint_conflicts(admin):
    _reset(admin)
    body = {"host": "203.0.113.10", "user": "stoic", "port": 22, "path": "/home/stoic/stoic",
            "accept_fingerprint": WRONG_FP}
    r = admin.post(f"{HM}/preflight", json=body, timeout=30)
    # Sidecar rejects: 409 (conflict) mapped by _call, or 502-class
    assert r.status_code in (409, 502, 500), r.text
    detail = r.json().get("detail", {})
    body_str = (detail.get("error") if isinstance(detail, dict) else str(detail)) or str(detail)
    assert "not confirmed" in body_str.lower() or "not confirmed" in str(detail).lower(), detail
    # No SSH-dependent facts (host_key.accepted not equal to WRONG_FP)
    st = _status(admin)
    # State should be failed (not awaiting install)
    assert st.get("status") == "failed" or st.get("awaiting") != "install"
    facts = st.get("facts") or {}
    hk = facts.get("host_key") or {}
    assert hk.get("accepted") != WRONG_FP


def test_preflight_exact_fingerprint_ok_and_journal(admin):
    _reset(admin)
    body = {"host": "203.0.113.10", "user": "stoic", "port": 22, "path": "/home/stoic/stoic",
            "accept_fingerprint": SIM_FP}
    r = admin.post(f"{HM}/preflight", json=body, timeout=60)
    assert r.status_code == 200, r.text
    facts = r.json()
    assert facts["ok"] is True
    assert facts["host_key"]["accepted"] == SIM_FP

    ev = admin.get(f"{HM}/events?limit=50", timeout=15).json()["events"]
    hka = [e for e in ev if e["action"] == "host_key_accepted"]
    assert hka, "no host_key_accepted event"
    det = hka[0]["detail"]
    assert det["accepted"] == SIM_FP
    assert det.get("observed"), "host_key_accepted event missing 'observed' fingerprints"


def test_preflight_without_stepup_bypass_403(admin):
    """Real step-up gate: no bypass header → 403 step_up_required/mfa_enrollment_required."""
    s = requests.Session()
    s.cookies.update(admin.cookies)
    s.headers.update({k: v for k, v in admin.headers.items() if k != "X-Step-Up-Bypass"})
    s.headers["X-Step-Up-Bypass"] = ""
    body = {"host": "203.0.113.10", "user": "stoic", "port": 22, "path": "/home/stoic/stoic",
            "accept_fingerprint": SIM_FP}
    r = s.post(f"{HM}/preflight", json=body, timeout=15)
    assert r.status_code == 403, r.text
    code = r.json()["detail"]["code"]
    assert code in ("step_up_required", "mfa_enrollment_required"), r.text


# ─── P1-2 · Cutover per-account gate + exception path ────────────────────────
def _drive_to_cutover_check(admin):
    _reset(admin)
    body = {"host": "203.0.113.10", "user": "stoic", "port": 22, "path": "/home/stoic/stoic",
            "accept_fingerprint": SIM_FP}
    assert admin.post(f"{HM}/preflight", json=body, timeout=60).status_code == 200
    assert admin.post(f"{HM}/start", json={}, timeout=15).status_code in (200, 202)
    _poll_until(admin, lambda s: s["awaiting"] == "freeze", timeout=60)
    assert admin.post(f"{HM}/advance", json={"step": "freeze"}, timeout=15).status_code in (200, 202)
    _poll_until(admin, lambda s: s["awaiting"] == "cutover_check", timeout=90)


def test_expected_accounts_snapshot_two_sim_accounts(admin):
    _drive_to_cutover_check(admin)
    st = _status(admin)
    exp = st.get("expected_accounts") or []
    assert len(exp) == 2, exp
    ids = {e["id"] for e in exp}
    assert ids == {"6a0000000000000000000001", "6a0000000000000000000002"}
    for e in exp:
        assert "account_number" in e
        assert "bridge_token_tail" in e
        assert e["bridge_token_tail"]


def test_cutover_missing_blocks_decommission_and_exception_unblocks(admin):
    st = _status(admin)
    if st.get("awaiting") != "cutover_check":
        _drive_to_cutover_check(admin)

    # Force a cutover_check when time%60 <= 20 to guarantee missing SIM-2
    # (simulated_run returns only SIM-1 heartbeat in that window).
    forced = None
    for _ in range(90):
        secs = int(time.time()) % 60
        if secs <= 18:  # give ourselves a small buffer
            r = admin.post(f"{HM}/advance", json={"step": "cutover_check"}, timeout=30)
            assert r.status_code in (200, 202), r.text
            _poll_until(admin, lambda s: s["status"] != "running", timeout=20)
            cut = (_status(admin).get("facts") or {}).get("cutover") or {}
            if cut.get("ok") is False and cut.get("missing_account_ids"):
                forced = cut
                break
        time.sleep(1)
    assert forced is not None, "could not force a failing cutover window"
    assert forced["arrived"] == 1
    assert forced["missing_account_ids"] == ["6a0000000000000000000002"]
    accounts = forced.get("accounts") or []
    assert len(accounts) == 2
    by_id = {a["id"]: a for a in accounts}
    assert by_id["6a0000000000000000000001"]["ok"] is True
    assert by_id["6a0000000000000000000002"]["ok"] is False

    # decommission must be BLOCKED with 409 while cutover not confirmed
    r = admin.post(f"{HM}/advance", json={"step": "decommission"}, timeout=15)
    assert r.status_code == 409, r.text
    body = str(r.json())
    assert "cutover not confirmed" in body.lower(), body

    # POST disable-missing (audited exception path) — with bypass → 200
    r = admin.post(f"{HM}/disable-missing", json={}, timeout=60)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data.get("disabled") == ["6a0000000000000000000002"]
    st = data["state"]
    cut = st["facts"]["cutover"]
    assert cut["ok"] is True, cut
    exc = st["facts"].get("cutover_exception")
    assert exc and exc["disabled_account_ids"] == ["6a0000000000000000000002"]

    # journal event
    ev = admin.get(f"{HM}/events?limit=50", timeout=15).json()["events"]
    assert any(e["action"] == "cutover_exception_disable_missing" for e in ev), \
        [e["action"] for e in ev[:15]]

    # Wait for state to become awaiting='decommission'. disable-missing calls
    # cutover_check synchronously; the gate transition to 'decommission' may
    # be immediate or require a further cutover_check advance depending on
    # sidecar timing.
    _await_decom = lambda s: s.get("awaiting") == "decommission"
    if not _await_decom(_status(admin)):
        r = admin.post(f"{HM}/advance", json={"step": "cutover_check"}, timeout=30)
        if r.status_code in (200, 202):
            _poll_until(admin, _await_decom, timeout=30)
    assert _await_decom(_status(admin)), _status(admin)

    # Now decommission proceeds → status done, key_material_destroyed_at set
    r = admin.post(f"{HM}/advance", json={"step": "decommission"}, timeout=15)
    assert r.status_code in (200, 202), r.text
    st = _poll_until(admin, lambda s: s["status"] == "done", timeout=30)
    assert st.get("key_material_destroyed_at"), st


def test_disable_missing_requires_step_up(admin):
    s = requests.Session()
    s.cookies.update(admin.cookies)
    s.headers.update({k: v for k, v in admin.headers.items() if k != "X-Step-Up-Bypass"})
    s.headers["X-Step-Up-Bypass"] = ""
    r = s.post(f"{HM}/disable-missing", json={}, timeout=15)
    assert r.status_code == 403, r.text


# ─── P1-5 · Dedicated migration token (not METRICS_TOKEN) ────────────────────
def test_migrator_token_is_dedicated_not_metrics_token():
    """Backend .env must expose MIGRATOR_TOKEN and it MUST differ from METRICS_TOKEN."""
    mig = os.environ.get("MIGRATOR_TOKEN", "")
    met = os.environ.get("METRICS_TOKEN", "")
    assert mig, "MIGRATOR_TOKEN is not set"
    if met:
        assert mig != met, "MIGRATOR_TOKEN must not equal METRICS_TOKEN (P1-5)"


# ─── P2-2 · Trust stats withheld until legal-approved; edge-probe 401 ────────
def test_public_trust_stats_unpublished_in_preview():
    r = requests.get(f"{API}/public/trust-stats", timeout=15)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("published") is False
    lr = body.get("legal_review", "")
    assert lr.startswith("pending"), lr
    ctx = body.get("context", "").lower()
    assert "not profitability" in ctx or "profitability" in ctx, body.get("context")


def test_public_edge_probe_401_without_token():
    r = requests.post(f"{API}/public/edge-probe",
                      json={}, headers={"Content-Type": "application/json"}, timeout=15)
    assert r.status_code == 401, r.text


def test_public_edge_probe_401_with_bogus_token():
    r = requests.post(f"{API}/public/edge-probe",
                      json={"token": "does-not-exist"},
                      headers={"Content-Type": "application/json"}, timeout=15)
    assert r.status_code == 401, r.text
