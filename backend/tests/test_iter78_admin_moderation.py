"""iter-78 · Admin Moderation (suspend / terminate) tests.

Exercises the live backend via REST (`requests`) + pymongo (sync) for DB
seeding/cleanup. Avoids motor/asyncio fixture pain. Matches pattern of
test_iter69_http_integration.py.

Covers:
  - GET  /api/terms                                (public)
  - POST /api/auth/login                           (blocks suspended + terminated)
  - POST /api/admin/users/{id}/suspend             (user, reason)
  - POST /api/admin/users/{id}/unsuspend
  - POST /api/admin/users/{id}/terminate
  - GET  /api/admin/users                          list filter
  - cannot suspend an admin / reason required
  - POST /api/admin/affiliates/{id}/{suspend,unsuspend,terminate}
  - termination forfeits unpaid balance + cancels pending payout
  - GET  /api/admin/audit-log
  - non-admin gets 403 on /api/admin/*
"""
from __future__ import annotations
import os
import uuid

import pytest
import requests
from bson import ObjectId
from pymongo import MongoClient

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    try:
        with open("/app/frontend/.env") as f:
            for line in f:
                if line.startswith("REACT_APP_BACKEND_URL"):
                    BASE_URL = line.split("=", 1)[1].strip().strip('"').rstrip("/")
    except Exception:
        BASE_URL = "http://localhost:8001"
if not BASE_URL.startswith("http"):
    BASE_URL = "https://" + BASE_URL

ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PASSWORD = "admin123"
TIMEOUT = 30


def _mongo():
    mongo_url = "mongodb://localhost:27017"
    db_name = "ai_trading_bot"
    with open("/app/backend/.env") as f:
        for line in f:
            if line.startswith("MONGO_URL="):
                mongo_url = line.split("=", 1)[1].strip().strip('"').strip("'")
            elif line.startswith("DB_NAME="):
                db_name = line.split("=", 1)[1].strip().strip('"').strip("'")
    return MongoClient(mongo_url)[db_name]


def _admin_session() -> requests.Session:
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
               timeout=TIMEOUT)
    assert r.status_code == 200, f"admin login: {r.status_code} {r.text}"
    return s


def _register() -> tuple[str, str, str]:
    """Register a user, mark email_verified so login works for tests."""
    suffix = uuid.uuid4().hex[:10]
    email = f"iter78_{suffix}@example.com"
    password = "password123"
    r = requests.post(f"{BASE_URL}/api/auth/register",
                      json={"email": email, "password": password,
                            "name": f"iter78-{suffix}",
                            "terms_agreed": True},
                      timeout=TIMEOUT)
    assert r.status_code == 200, f"register {email}: {r.status_code} {r.text}"
    uid = r.json()["id"]
    # Tests bypass the email-verification gate by flipping the flag directly.
    _mongo().users.update_one(
        {"_id": ObjectId(uid)},
        {"$set": {"email_verified": True},
         "$unset": {"activation_token": "", "activation_expires_at": ""}},
    )
    return email, password, uid


@pytest.fixture
def cleanup():
    refs: dict = {"users": [], "affiliates": [], "bot_configs": [],
                  "payout_requests": [], "audit_target_ids": []}
    yield refs
    db = _mongo()
    if refs["users"]:
        db.users.delete_many({"_id": {"$in": refs["users"]}})
    if refs["affiliates"]:
        db.affiliates.delete_many({"_id": {"$in": refs["affiliates"]}})
    if refs["bot_configs"]:
        db.bot_configs.delete_many({"_id": {"$in": refs["bot_configs"]}})
    if refs["payout_requests"]:
        db.affiliate_payout_requests.delete_many(
            {"_id": {"$in": refs["payout_requests"]}}
        )
    if refs["audit_target_ids"]:
        db.admin_audit_log.delete_many(
            {"target_id": {"$in": refs["audit_target_ids"]}}
        )


# ─────────────────── Public terms ───────────────────
def test_public_terms_returns_markdown():
    r = requests.get(f"{BASE_URL}/api/terms", timeout=TIMEOUT)
    assert r.status_code == 200
    body = r.json()
    assert body.get("version")
    assert "STOIC" in body.get("markdown", "")
    assert "Suspension" in body.get("markdown", "")


# ─────────────────── Non-admin guard ───────────────────
def test_non_admin_cannot_call_admin_endpoints(cleanup):
    email, password, uid = _register()
    cleanup["users"].append(ObjectId(uid))

    s = requests.Session()
    s.post(f"{BASE_URL}/api/auth/login",
           json={"email": email, "password": password}, timeout=TIMEOUT)
    for path in ("/api/admin/users", "/api/admin/audit-log",
                 "/api/admin/affiliates"):
        r = s.get(f"{BASE_URL}{path}", timeout=TIMEOUT)
        assert r.status_code == 403, f"{path}: {r.status_code}"


# ─────────────────── Suspend / unsuspend ───────────────────
def test_suspend_blocks_login_and_unsuspend_restores(cleanup):
    admin = _admin_session()
    email, password, uid = _register()
    cleanup["users"].append(ObjectId(uid))
    cleanup["audit_target_ids"].append(uid)

    r = admin.post(f"{BASE_URL}/api/admin/users/{uid}/suspend",
                   json={"reason": "spam violations"}, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["user"]["status"] == "suspended"
    assert body["user"]["suspension_reason"] == "spam violations"

    login = requests.post(f"{BASE_URL}/api/auth/login",
                          json={"email": email, "password": password},
                          timeout=TIMEOUT)
    assert login.status_code == 403
    detail = login.json().get("detail")
    assert isinstance(detail, dict)
    assert detail.get("code") == "account_suspended"

    audit_r = admin.get(f"{BASE_URL}/api/admin/audit-log?kind=user&limit=100",
                        timeout=TIMEOUT)
    assert audit_r.status_code == 200
    audit = audit_r.json()["audit"]
    assert any(a["target_id"] == uid and a["action"] == "suspend" for a in audit)

    r2 = admin.post(f"{BASE_URL}/api/admin/users/{uid}/unsuspend", timeout=TIMEOUT)
    assert r2.status_code == 200
    assert r2.json()["user"]["status"] == "active"

    login2 = requests.post(f"{BASE_URL}/api/auth/login",
                           json={"email": email, "password": password},
                           timeout=TIMEOUT)
    assert login2.status_code == 200


def test_suspend_requires_reason(cleanup):
    admin = _admin_session()
    email, password, uid = _register()
    cleanup["users"].append(ObjectId(uid))

    r = admin.post(f"{BASE_URL}/api/admin/users/{uid}/suspend",
                   json={"reason": "   "}, timeout=TIMEOUT)
    assert r.status_code == 400


def test_cannot_suspend_an_admin():
    admin = _admin_session()
    me = admin.get(f"{BASE_URL}/api/auth/me", timeout=TIMEOUT).json()
    r = admin.post(f"{BASE_URL}/api/admin/users/{me['id']}/suspend",
                   json={"reason": "self"}, timeout=TIMEOUT)
    assert r.status_code == 400
    assert "admin" in r.json()["detail"].lower()


# ─────────────────── Terminate ───────────────────
def test_terminate_blocks_login_and_disables_bot_and_affiliate(cleanup):
    admin = _admin_session()
    email, password, uid = _register()
    cleanup["users"].append(ObjectId(uid))
    cleanup["audit_target_ids"].append(uid)

    db = _mongo()
    db.bot_configs.update_one(
        {"user_id": uid, "account_id": None},
        {"$set": {"active": True, "risk_level": "medium",
                  "symbols": ["XAUUSD"]}},
        upsert=True,
    )
    cfg_doc = db.bot_configs.find_one({"user_id": uid, "account_id": None})
    cfg_id = cfg_doc["_id"]
    cleanup["bot_configs"].append(cfg_id)

    aff_id = db.affiliates.insert_one({
        "user_id": uid, "user_email": email,
        "code": f"T78{uuid.uuid4().hex[:3].upper()}",
        "active": True, "unpaid_balance_usd": 50.0,
    }).inserted_id
    cleanup["affiliates"].append(aff_id)
    cleanup["audit_target_ids"].append(str(aff_id))

    r = admin.post(f"{BASE_URL}/api/admin/users/{uid}/terminate",
                   json={"reason": "affiliate fraud"}, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    assert r.json()["user"]["status"] == "terminated"

    fresh_cfg = db.bot_configs.find_one({"_id": cfg_id})
    assert fresh_cfg["active"] is False
    assert fresh_cfg.get("deactivated_reason") == "user_terminated"

    fresh_aff = db.affiliates.find_one({"_id": aff_id})
    assert fresh_aff["active"] is False
    assert fresh_aff.get("terminated") is True

    login = requests.post(f"{BASE_URL}/api/auth/login",
                          json={"email": email, "password": password},
                          timeout=TIMEOUT)
    assert login.status_code == 403
    assert login.json()["detail"]["code"] == "account_terminated"


# ─────────────────── Affiliate moderation ───────────────────
def test_affiliate_terminate_forfeits_balance_and_cancels_payouts(cleanup):
    admin = _admin_session()
    email, password, uid = _register()
    cleanup["users"].append(ObjectId(uid))

    db = _mongo()
    aff_id = db.affiliates.insert_one({
        "user_id": uid, "user_email": email,
        "code": f"T78A{uuid.uuid4().hex[:2].upper()}",
        "active": True, "unpaid_balance_usd": 150.0,
    }).inserted_id
    cleanup["affiliates"].append(aff_id)
    cleanup["audit_target_ids"].append(str(aff_id))

    payout_id = db.affiliate_payout_requests.insert_one({
        "affiliate_id": str(aff_id), "user_id": uid,
        "amount_usd": 150.0, "status": "pending",
    }).inserted_id
    cleanup["payout_requests"].append(payout_id)

    r = admin.post(f"{BASE_URL}/api/admin/affiliates/{aff_id}/terminate",
                   json={"reason": "fake clicks"}, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    body = r.json()["affiliate"]
    assert body["terminated"] is True
    assert body["unpaid_balance_usd"] == 0.0
    assert body["forfeited_balance_usd"] == 150.0

    fresh = db.affiliate_payout_requests.find_one({"_id": payout_id})
    assert fresh["status"] == "cancelled"


def test_affiliate_suspend_then_unsuspend(cleanup):
    admin = _admin_session()
    email, password, uid = _register()
    cleanup["users"].append(ObjectId(uid))

    db = _mongo()
    aff_id = db.affiliates.insert_one({
        "user_id": uid, "user_email": email,
        "code": f"T78B{uuid.uuid4().hex[:2].upper()}",
        "active": True, "unpaid_balance_usd": 10.0,
    }).inserted_id
    cleanup["affiliates"].append(aff_id)
    cleanup["audit_target_ids"].append(str(aff_id))

    r = admin.post(f"{BASE_URL}/api/admin/affiliates/{aff_id}/suspend",
                   json={"reason": "policy review"}, timeout=TIMEOUT)
    assert r.status_code == 200
    assert r.json()["affiliate"]["active"] is False

    r2 = admin.post(f"{BASE_URL}/api/admin/affiliates/{aff_id}/unsuspend",
                    timeout=TIMEOUT)
    assert r2.status_code == 200
    assert r2.json()["affiliate"]["active"] is True


# ─────────────────── Admin user listing ───────────────────
def test_admin_user_listing_filters_status():
    admin = _admin_session()
    r = admin.get(f"{BASE_URL}/api/admin/users?status=active&limit=5",
                  timeout=TIMEOUT)
    assert r.status_code == 200
    body = r.json()
    assert "users" in body
    for u in body["users"]:
        assert (u.get("status") or "active") == "active"
