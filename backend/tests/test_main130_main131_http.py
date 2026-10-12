"""HTTP tests for main130/main131 review fixes against preview backend.

Covers P2-04 /api/health release, P2-02 /api/trades/predictions,
P1-02 /api/ops/go-live-checklist, P1-01 account delete + admin force-delete,
M3 wizard use-reported-login refusal.
"""
import os
import random
import string
import pytest
import requests
from pymongo import MongoClient
from bson import ObjectId

pytestmark = pytest.mark.http

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://stoic-trading-bot.preview.emergentagent.com").rstrip("/")
# Read admin pw from backend .env file at runtime
_ENV = {}
with open("/app/backend/.env") as f:
    for ln in f:
        if "=" in ln and not ln.strip().startswith("#"):
            k, _, v = ln.strip().partition("=")
            _ENV[k] = v.strip('"').strip("'")
# Try several admin pw sources
ADMIN_EMAIL = "admin@stoicaibot.com"
# Read pw from test_credentials.md (file is the source of truth)
import re
with open("/app/memory/test_credentials.md") as f:
    _creds = f.read()
m = re.search(r"Password:\s*(\S+)", _creds)
assert m, "admin password not found in test_credentials.md"
ADMIN_PASSWORD = m.group(1)
STEP_UP_BYPASS = _ENV["STEP_UP_BYPASS_TOKEN"]
MONGO_URL = _ENV["MONGO_URL"]
DB_NAME = _ENV["DB_NAME"]


@pytest.fixture(scope="module")
def mongo():
    client = MongoClient(MONGO_URL)
    yield client[DB_NAME]
    client.close()


@pytest.fixture(scope="module")
def admin_sess():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
               timeout=30)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    csrf = s.cookies.get("csrf_token")
    assert csrf, "no csrf cookie after login"
    s.headers.update({"X-CSRF-Token": csrf})
    return s


@pytest.fixture(scope="module")
def admin_user_id(admin_sess, mongo):
    r = admin_sess.get(f"{BASE_URL}/api/auth/me", timeout=15)
    assert r.status_code == 200
    data = r.json()
    uid = data.get("id") or data.get("user_id") or (data.get("user") or {}).get("id")
    if uid:
        return uid
    # fallback: find in mongo
    u = mongo.users.find_one({"email": ADMIN_EMAIL})
    assert u, "admin user not found"
    return u.get("id") or str(u["_id"])


# ---------- P2-04: /api/health release block ----------
def test_health_release_block():
    r = requests.get(f"{BASE_URL}/api/health", timeout=15)
    assert r.status_code == 200, r.text
    j = r.json()
    rel = j.get("release")
    assert isinstance(rel, dict), f"release missing: {j}"
    for k in ("tag", "commit", "short_commit", "attested", "ea_version",
             "policy_version", "model_manifest", "id"):
        assert k in rel, f"release missing key {k}: {rel}"
    assert rel["ea_version"] == "1.64", rel["ea_version"]
    mm = rel["model_manifest"]
    assert mm.get("version") == 2, mm
    for k in ("version", "code_commit", "sha256"):
        assert k in mm
    assert isinstance(rel["id"], str) and rel["id"]


# ---------- P2-02: /api/trades/predictions ----------
def test_predictions_shape(admin_sess):
    r = admin_sess.get(f"{BASE_URL}/api/trades/predictions",
                       params={"symbol": "XAUUSD"}, timeout=30)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j.get("horizon_hours") == 24
    assert j.get("horizon_bars") == 96
    prov = j.get("provenance")
    assert isinstance(prov, dict)
    assert prov.get("label") == "Prediction calibration — not broker performance"
    for k in ("source", "as_of", "same_bar_rule", "counts"):
        assert k in prov, f"missing provenance.{k}"
    counts = prov["counts"]
    for k in ("decided", "excluded_no_data", "expired", "pending"):
        assert k in counts
    assert counts["pending"] == j.get("pending")


def test_predictions_symbol_validation(admin_sess):
    r = admin_sess.get(f"{BASE_URL}/api/trades/predictions",
                       params={"symbol": "../x"}, timeout=15)
    assert r.status_code == 422, f"expected 422, got {r.status_code}: {r.text}"


# ---------- P1-02: go-live checklist ----------
def test_go_live_checklist_release_signed(admin_sess):
    r = admin_sess.get(f"{BASE_URL}/api/ops/go-live-checklist", timeout=30)
    assert r.status_code == 200, r.text
    j = r.json()
    rows = j.get("rows") or []
    row = next((x for x in rows if x.get("id") == "release_signed"), None)
    assert row, f"release_signed row missing. rows ids: {[x.get('id') for x in rows]}"
    assert row.get("ok") is False
    detail = row.get("detail") or ""
    assert detail.startswith("missing:"), f"detail: {detail!r}"
    checks = row.get("checks")
    assert isinstance(checks, dict)
    for k in ("ea_verified", "ea_enforced", "attestation_valid", "build_matches_release"):
        assert k in checks, f"missing checks.{k}"


# ---------- P1-01: account delete flow ----------
def _rand_acc_num():
    return "".join(random.choice(string.digits) for _ in range(7))


def _create_account(sess, label="P1-01 test"):
    payload = {
        "broker": "OnEquity",
        "mode": "live",
        "account_number": _rand_acc_num(),
        "label": label,
        "demo": True,
    }
    r = sess.post(f"{BASE_URL}/api/accounts", json=payload, timeout=30)
    assert r.status_code in (200, 201), f"create account failed: {r.status_code} {r.text}"
    j = r.json()
    aid = j.get("id") or j.get("_id") or (j.get("account") or {}).get("id")
    assert aid, f"no id in response: {j}"
    return aid


def test_delete_untouched_account(admin_sess):
    aid = _create_account(admin_sess, "P1-01 untouched")
    r = admin_sess.delete(f"{BASE_URL}/api/accounts/{aid}", timeout=30)
    assert r.status_code == 200, f"{r.status_code} {r.text}"


def test_delete_account_with_trade_refuses_and_force_delete(admin_sess, admin_user_id, mongo):
    aid = _create_account(admin_sess, "P1-01 withtrade")
    # Insert a fake open trade attached to this account
    trade_doc = {
        "account_id": aid,
        "user_id": admin_user_id,
        "status": "open",
        "symbol": "XAUUSD",
        "_test_marker": "main130_main131_http",
    }
    ins = mongo.trades.insert_one(trade_doc)
    trade_id = ins.inserted_id
    try:
        # (a) plain DELETE → 409 trades_attached
        r = admin_sess.delete(f"{BASE_URL}/api/accounts/{aid}", timeout=30)
        assert r.status_code == 409, f"{r.status_code} {r.text}"
        body = r.json()
        detail = body.get("detail") or body
        code = detail.get("code") if isinstance(detail, dict) else None
        assert code == "trades_attached", f"detail: {detail}"

        # (b) ?force=true → still 409
        r = admin_sess.delete(f"{BASE_URL}/api/accounts/{aid}", params={"force": "true"}, timeout=30)
        assert r.status_code == 409, f"force path: {r.status_code} {r.text}"

        # (c) force-delete endpoint validations
        hdr = {"X-Step-Up-Bypass": STEP_UP_BYPASS}
        # disposition mismatch (claim no positions but trade is still open)
        r = admin_sess.post(f"{BASE_URL}/api/admin/accounts/{aid}/force-delete",
                            json={"disposition": "no_positions",
                                  "reason": "testing agent verification of force delete"},
                            headers=hdr, timeout=30)
        assert r.status_code == 409, f"no_positions path: {r.status_code} {r.text}"
        body = r.json()
        detail = body.get("detail") or body
        code = detail.get("code") if isinstance(detail, dict) else None
        assert code == "disposition_mismatch", f"detail: {detail}"

        # bogus disposition → 422 disposition_required/invalid
        r = admin_sess.post(f"{BASE_URL}/api/admin/accounts/{aid}/force-delete",
                            json={"disposition": "bogus",
                                  "reason": "testing agent verification"},
                            headers=hdr, timeout=30)
        assert r.status_code == 422, f"bogus: {r.status_code} {r.text}"

        # reason too short → 422
        r = admin_sess.post(f"{BASE_URL}/api/admin/accounts/{aid}/force-delete",
                            json={"disposition": "positions_closed_on_broker",
                                  "reason": "short"},
                            headers=hdr, timeout=30)
        assert r.status_code == 422, f"short reason: {r.status_code} {r.text}"

        # valid → 200
        r = admin_sess.post(f"{BASE_URL}/api/admin/accounts/{aid}/force-delete",
                            json={"disposition": "positions_closed_on_broker",
                                  "reason": "testing agent verification of force delete"},
                            headers=hdr, timeout=30)
        assert r.status_code == 200, f"valid force-delete: {r.status_code} {r.text}"
        j = r.json()
        assert j.get("ok") is True
        assert j.get("disposition") == "positions_closed_on_broker"
        assert "counts_after_reconciliation" in j

        # Verify account no longer listed
        r = admin_sess.get(f"{BASE_URL}/api/accounts", timeout=30)
        assert r.status_code == 200
        accs = r.json()
        acc_list = accs if isinstance(accs, list) else (accs.get("accounts") or accs.get("items") or [])
        ids = [a.get("id") or a.get("_id") for a in acc_list]
        assert aid not in ids, f"account still listed: {ids}"

        # Trade doc should now be status=closed with close_reason admin_force_delete
        td = mongo.trades.find_one({"_id": trade_id})
        assert td, "trade doc disappeared"
        assert td.get("status") == "closed", f"status: {td.get('status')}"
        assert td.get("close_reason") == "admin_force_delete:positions_closed_on_broker", \
            f"close_reason: {td.get('close_reason')}"

        # Audit log entry present
        audit = mongo.admin_audit_log.find_one({
            "action": "account_force_deleted",
            "target_id": aid,
        })
        assert audit, "admin_audit_log entry missing"
    finally:
        # Clean up: remove our test trade (whatever state it's in)
        mongo.trades.delete_one({"_id": trade_id})


# ---------- M3: wizard use-reported-login refusal ----------
def test_wizard_use_reported_login_refuses_when_no_mismatch(admin_sess):
    # Create a fresh account; its ea_identity will NOT contain the required
    # "MT5 login mismatch: EA reports N" marker → endpoint must 409 no_reported_login.
    aid = _create_account(admin_sess, "M3 no-mismatch")
    try:
        r = admin_sess.post(f"{BASE_URL}/api/setup/wizard/{aid}/use-reported-login",
                            timeout=30)
        assert r.status_code == 409, f"{r.status_code} {r.text}"
        body = r.json()
        detail = body.get("detail") or body
        code = detail.get("code") if isinstance(detail, dict) else None
        assert code == "no_reported_login", f"detail: {detail}"
    finally:
        # account was never connected → plain delete must succeed
        try:
            admin_sess.delete(f"{BASE_URL}/api/accounts/{aid}", timeout=15)
        except Exception:
            pass
