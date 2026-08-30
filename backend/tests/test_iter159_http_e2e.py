"""HTTP E2E tests for iter-159 Soak Tracker & Release Canary against the public preview URL.

Covers:
  - Admin login (cookie + CSRF)
  - GET /api/ops/soak/status -> countdown block
  - POST /api/ops/soak/checkpoint (recorded_by=manual)
  - GET /api/command-center/status -> canary + soak sections
  - GET /api/command-center/evidence-export -> exact section list + hash chain
  - Canary lifecycle: reject LIVE, accept demo, status verdict, evaluate (halted:false),
    resume-when-disabled -> 400, disable (leaves it OFF at the end)
  - Non-admin -> 403 on ops endpoints
"""
import os
import time
import requests
import pytest

from live_target import require_live_base_url
BASE_URL = require_live_base_url()

ADMIN_EMAIL = "admin@stoicaibot.com"
ADMIN_PASSWORD = "admin123"
NONADMIN_EMAIL = "ccnon_11613bd0@example.com"
NONADMIN_PASSWORD = "Kd5#Zt9mW2xVpR7c"
DEMO_ACCOUNT_ID = "6a39653e0760995b7e966183"


def _login(email, password):
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login", json={"email": email, "password": password}, timeout=30)
    assert r.status_code == 200, f"login failed for {email}: {r.status_code} {r.text[:200]}"
    csrf = s.cookies.get("csrf_token")
    if csrf:
        s.headers.update({"X-CSRF-Token": csrf})
    return s


@pytest.fixture(scope="module")
def admin():
    return _login(ADMIN_EMAIL, ADMIN_PASSWORD)


@pytest.fixture(scope="module")
def nonadmin():
    return _login(NONADMIN_EMAIL, NONADMIN_PASSWORD)


# ============ SOAK ============
def test_soak_status_has_countdown(admin):
    r = admin.get(f"{BASE_URL}/api/ops/soak/status", timeout=20)
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert "countdown" in body, f"missing countdown: {body}"
    cd = body["countdown"]
    for k in ("day", "days_target", "days_remaining", "hours_into_day", "today_checkpoint_done", "ends_at", "missed_days"):
        assert k in cd, f"countdown missing {k}: {cd}"
    assert cd["days_target"] == 14


def test_soak_checkpoint_manual(admin):
    # idempotent - if already recorded today it should still 200 or 409-ish; accept success
    r = admin.post(f"{BASE_URL}/api/ops/soak/checkpoint", json={}, timeout=30)
    assert r.status_code in (200, 201), r.text[:300]
    # Verify status now shows today_checkpoint_done True
    r2 = admin.get(f"{BASE_URL}/api/ops/soak/status", timeout=20)
    assert r2.status_code == 200
    assert r2.json()["countdown"]["today_checkpoint_done"] is True


# ============ COMMAND CENTER ============
def test_command_center_status_sections(admin):
    r = admin.get(f"{BASE_URL}/api/command-center/status", timeout=30)
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    sections = body.get("sections") or body
    # canary present
    assert "canary" in sections or any("canary" in str(k) for k in sections.keys()), f"no canary: {list(sections.keys())[:20]}"
    canary = sections["canary"] if "canary" in sections else None
    assert canary is not None
    # soak section carries countdown fields
    soak = sections.get("soak") or sections.get("soak_status")
    assert soak is not None, f"no soak section: {list(sections.keys())}"
    detail = soak.get("detail", soak)
    # days_remaining/ends_at/today_checkpoint_done should be somewhere in soak section
    flat = str(soak)
    for k in ("days_remaining", "ends_at", "today_checkpoint_done"):
        assert k in flat, f"soak missing {k}: {soak}"


def test_evidence_export_sections_exact(admin):
    import hashlib, json as _json
    r = admin.get(f"{BASE_URL}/api/command-center/evidence-export", timeout=45)
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    sections = body.get("sections") or []
    names = [s.get("section") for s in sections]
    expected = ["provenance", "certifications", "soak_status", "soak_evidence_chain", "guard_health", "release_canary"]
    assert names == expected, f"expected {expected}, got {names}"
    # Verify hash chain: each section.hash = sha256(canonical JSON of record minus hash/_id)
    # and section.prev_hash chains to previous section's hash.
    prev = "genesis"
    for s in sections:
        assert s.get("prev_hash", "") == prev, f"prev_hash chain broken at {s.get('section')}"
        rec = {k: v for k, v in s.items() if k not in ("hash", "_id")}
        canonical = _json.dumps(rec, sort_keys=True, separators=(",", ":"))
        expected_hash = hashlib.sha256((prev + canonical).encode("utf-8")).hexdigest()
        assert s["hash"] == expected_hash, f"section {s.get('section')} hash mismatch"
        prev = s["hash"]
    # report_hash = sha256(concat of section hashes)
    report_hash = hashlib.sha256("".join(s["hash"] for s in sections).encode("utf-8")).hexdigest()
    assert body.get("report_hash") == report_hash, f"report_hash mismatch expected {report_hash} got {body.get('report_hash')}"


# ============ CANARY LIFECYCLE ============
def test_canary_reject_live_and_accept_demo_flow(admin):
    # Ensure disabled at start
    admin.post(f"{BASE_URL}/api/ops/canary/disable", json={}, timeout=15)

    # 1) resume when disabled -> 400
    r_res = admin.post(f"{BASE_URL}/api/ops/canary/resume", json={}, timeout=15)
    assert r_res.status_code == 400, f"expected 400 resume-when-disabled, got {r_res.status_code}: {r_res.text[:200]}"

    # 2) Try to find a LIVE account to reject
    live_id = None
    try:
        r_accs = admin.get(f"{BASE_URL}/api/accounts", timeout=20)
        if r_accs.status_code == 200:
            arr = r_accs.json()
            if isinstance(arr, dict):
                arr = arr.get("accounts") or arr.get("data") or []
            for a in arr:
                atype = (a.get("account_type") or "").lower()
                be = (a.get("broker_environment") or "").upper()
                if atype == "live" or be == "LIVE":
                    live_id = a.get("id") or a.get("_id") or a.get("account_id")
                    if live_id:
                        break
    except Exception:
        pass
    if live_id:
        r_bad = admin.post(f"{BASE_URL}/api/ops/canary/enable", json={"account_id": live_id}, timeout=20)
        assert r_bad.status_code == 400, f"live account should be rejected, got {r_bad.status_code}: {r_bad.text[:200]}"

    # 3) Enable with demo account
    r_en = admin.post(f"{BASE_URL}/api/ops/canary/enable", json={"account_id": DEMO_ACCOUNT_ID}, timeout=30)
    assert r_en.status_code in (200, 201), f"enable failed: {r_en.status_code} {r_en.text[:300]}"

    # 4) Status shows enabled + verdict insufficient_evidence
    r_st = admin.get(f"{BASE_URL}/api/ops/canary/status", timeout=20)
    assert r_st.status_code == 200
    st = r_st.json()
    assert st.get("enabled") is True, f"not enabled: {st}"
    verdict = st.get("verdict") or st.get("divergence_verdict") or {}
    vstr = str(verdict).lower()
    assert "insufficient" in vstr, f"expected insufficient evidence, got {verdict}"

    # 5) Evaluate -> halted:false
    r_ev = admin.post(f"{BASE_URL}/api/ops/canary/evaluate", json={}, timeout=30)
    assert r_ev.status_code == 200, r_ev.text[:300]
    ev = r_ev.json()
    assert ev.get("halted") is False, f"should not halt with insufficient evidence: {ev}"

    # 6) Disable — leave OFF at end
    r_dis = admin.post(f"{BASE_URL}/api/ops/canary/disable", json={}, timeout=20)
    assert r_dis.status_code in (200, 204), r_dis.text[:200]
    r_st2 = admin.get(f"{BASE_URL}/api/ops/canary/status", timeout=15)
    assert r_st2.json().get("enabled") is False, "canary should be disabled at end"


# ============ ADMIN GATING ============
@pytest.mark.parametrize("method,path,body", [
    ("POST", "/api/ops/soak/start", {}),
    ("POST", "/api/ops/soak/checkpoint", {}),
    ("POST", "/api/ops/soak/reset", {}),
    ("GET", "/api/ops/soak/status", None),
    ("POST", "/api/ops/canary/enable", {"account_id": DEMO_ACCOUNT_ID}),
    ("POST", "/api/ops/canary/disable", {}),
    ("POST", "/api/ops/canary/evaluate", {}),
    ("POST", "/api/ops/canary/resume", {}),
    ("GET", "/api/ops/canary/status", None),
])
def test_nonadmin_403(nonadmin, method, path, body):
    url = f"{BASE_URL}{path}"
    if method == "GET":
        r = nonadmin.get(url, timeout=20)
    else:
        r = nonadmin.post(url, json=body or {}, timeout=20)
    assert r.status_code == 403, f"{method} {path} expected 403 got {r.status_code}: {r.text[:200]}"


pytestmark = pytest.mark.http
