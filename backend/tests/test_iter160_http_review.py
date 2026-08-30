"""iter-160 HTTP-level review tests against the live preview backend.

Covers the endpoints in the review request:
- GET  /api/accounts/overview
- GET  /api/bot/health-score
- GET  /api/authority
- POST /api/accounts/{id}/test-trade
- GET  /api/certification/center
- POST /api/certification/center/issue
- GET  /api/auth/audit + PUT /api/bot/config diff

Runs against REACT_APP_BACKEND_URL from /app/frontend/.env with the admin
cookie/CSRF flow. Read-only + one benign bot-config toggle we restore.
"""
import os
import re
import pytest
import requests

BASE_URL = (
    os.environ.get("REACT_APP_BACKEND_URL")
    or open("/app/frontend/.env").read().split("REACT_APP_BACKEND_URL=")[1].splitlines()[0].strip()
).rstrip("/")

ADMIN_EMAIL = "admin@stoicaibot.com"
ADMIN_PASS = "admin123"


# ---------------------------------------------------------------- fixtures
@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASS},
               timeout=20)
    assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text[:200]}"
    csrf = s.cookies.get("csrf_token")
    assert csrf, "csrf_token cookie not set on login"
    s.headers.update({"X-CSRF-Token": csrf, "Content-Type": "application/json"})
    return s


# ---------------------------------------------------- P0-1 accounts overview
def test_accounts_overview_shape(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/accounts/overview", timeout=20)
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    tot = body.get("totals") or {}
    for k in ("accounts", "trading_enabled", "bots_enabled", "connected"):
        assert k in tot, f"totals.{k} missing: {tot}"
        assert isinstance(tot[k], int)
    for row in body.get("accounts") or []:
        assert "bot_enabled" in row
        cs = row.get("connection_state") or {}
        assert cs.get("threshold_seconds") == 180
        for k in ("state", "heartbeat_age_seconds", "reason", "evaluated_at"):
            assert k in cs, f"connection_state.{k} missing: {cs}"


# ---------------------------------------------------- P0-3 health score caps
def test_bot_health_hard_caps(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/bot/health-score", timeout=20)
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert "hard_caps" in body, f"missing hard_caps: {list(body)[:10]}"
    assert isinstance(body["hard_caps"], list)
    # Admin has unacked critical alerts + unknown executions → must be capped.
    if body["hard_caps"]:
        assert body["score"] <= 60, f"score {body['score']} despite hard caps {body['hard_caps']}"
        assert body["status"] in {"critical", "degraded", "warning", "poor"}
        assert body["status"] != "excellent"
        labels = " ".join((i.get("label") or "") for i in body.get("issues") or [])
        assert "HARD CAP" in labels.upper() or any(
            "HARD CAP" in (i.get("message") or "").upper()
            for i in body.get("issues") or []
        ), "expected HARD CAP labelling in issues[]"


# ---------------------------------------------------- P0-4 authority pill
def test_authority_shape(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/authority", timeout=20)
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    pt = ((body.get("domains") or {}).get("position_truth")) or {}
    assert pt.get("level") in {"FULL", "STALE", "UNKNOWN", "CONFLICTED", "PARTIAL"}


# ---------------------------------------------------- P0-5 force trade gate
def test_force_trade_gate(admin_session):
    ov = admin_session.get(f"{BASE_URL}/api/accounts/overview", timeout=20).json()
    accts = ov.get("accounts") or []
    if not accts:
        pytest.skip("no accounts on admin to exercise test-trade gate")
    acc = accts[0]
    acc_id = acc.get("id") or acc.get("_id") or acc.get("account_id")
    assert acc_id, f"no id on account row: {list(acc)[:8]}"
    r = admin_session.post(f"{BASE_URL}/api/accounts/{acc_id}/test-trade",
                           json={}, timeout=20)
    # Must never be 200 without full certification; expect 409 or 400 with a code.
    assert r.status_code in (400, 409, 403), f"unexpected {r.status_code} {r.text[:200]}"
    try:
        detail = r.json().get("detail") or r.json()
    except Exception:
        detail = {}
    if r.status_code == 409:
        code = (detail or {}).get("code") if isinstance(detail, dict) else None
        assert code in {"force_trade_not_certified", "stale_ea", "ea_never_connected"}, detail


# ---------------------------------------------------- P0-6 certification centre
def test_certification_center_shape(admin_session):
    ov = admin_session.get(f"{BASE_URL}/api/accounts/overview", timeout=20).json()
    accts = ov.get("accounts") or []
    if not accts:
        pytest.skip("no accounts to inspect certification centre")
    acc = accts[0]
    acc_id = acc.get("id") or acc.get("_id") or acc.get("account_id")
    r = admin_session.get(
        f"{BASE_URL}/api/certification/center",
        params={"account_id": acc_id},
        timeout=20,
    )
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    for k in ("issuable", "live_certified", "missing_mandatory", "certification_note"):
        assert k in body, f"cert center missing {k}: {list(body)[:12]}"
    # overall_score should divide by all 6 pillars, so it should be <= 100.
    if body.get("overall_score") is not None:
        assert 0 <= float(body["overall_score"]) <= 100


def test_certification_issue_rejects_when_mandatory_missing(admin_session):
    ov = admin_session.get(f"{BASE_URL}/api/accounts/overview", timeout=20).json()
    accts = ov.get("accounts") or []
    if not accts:
        pytest.skip("no accounts to exercise issue endpoint")
    acc_id = accts[0].get("id") or accts[0].get("_id")
    cc = admin_session.get(
        f"{BASE_URL}/api/certification/center",
        params={"account_id": acc_id},
        timeout=20,
    ).json()
    if cc.get("issuable"):
        pytest.skip("account is already issuable — cannot assert 400 refusal here")
    r = admin_session.post(
        f"{BASE_URL}/api/certification/center/issue",
        json={"account_id": acc_id},
        timeout=20,
    )
    assert r.status_code in (400, 403, 409, 429), f"expected refusal, got {r.status_code} {r.text[:200]}"


# ---------------------------------------------------- P1-8 audit diff
def test_audit_records_actor_and_diff(admin_session):
    # read current bot config value
    cfg = admin_session.get(f"{BASE_URL}/api/bot/config", timeout=20)
    assert cfg.status_code == 200, cfg.text[:200]
    current = cfg.json() or {}
    original = current.get("loss_cooldown_minutes")
    new_val = 17 if original != 17 else 23
    payload = dict(current)
    payload["loss_cooldown_minutes"] = new_val
    put = admin_session.put(f"{BASE_URL}/api/bot/config",
                            json=payload, timeout=20)
    assert put.status_code in (200, 204), put.text[:200]
    try:
        aud = admin_session.get(f"{BASE_URL}/api/auth/audit",
                                params={"limit": 25}, timeout=20)
        assert aud.status_code == 200, aud.text[:200]
        entries = aud.json()
        if isinstance(entries, dict):
            entries = entries.get("entries") or entries.get("items") or []
        assert entries, "audit list empty"
        newest = next(
            (e for e in entries
             if "config" in (e.get("event") or e.get("action") or "").lower()),
            entries[0])
        assert newest.get("actor") or newest.get("actor_id") or newest.get("user_id"), \
            f"actor missing on audit entry: {list(newest)[:12]}"
        det = newest.get("detail") or newest.get("meta") or {}
        changed = det.get("changed") if isinstance(det, dict) else None
        if changed is not None:
            # only changed fields should appear
            assert "loss_cooldown_minutes" in changed, changed
            entry = changed["loss_cooldown_minutes"]
            assert "from" in entry and "to" in entry
            assert entry["to"] == new_val
    finally:
        # restore
        if original is not None:
            payload["loss_cooldown_minutes"] = original
            admin_session.put(f"{BASE_URL}/api/bot/config",
                              json=payload, timeout=20)


# ---------------------------------------------------- P1-10 status dimensions
def test_status_reports_live_dimensions(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/status", timeout=20)
    assert r.status_code == 200, r.text[:200]
    body = r.json()
    dims = body.get("dimensions") or body.get("services") or body.get("components") or {}
    assert dims, f"no dimensions/services/components in /api/status: {list(body)[:12]}"
    keys = {k.lower() for k in (dims.keys() if isinstance(dims, dict)
                                else [d.get("name","") for d in dims])}
    # Expect at least a couple of core service dimensions.
    assert len(keys) >= 2, f"expected multiple live components, got: {keys}"
