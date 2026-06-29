"""iter-79 · Email Activation + Terms Acceptance tests.

Validates the new signup flow:
  1. POST /api/auth/register requires `terms_agreed=true` → else 400
  2. Register creates user with email_verified=false + activation_token
  3. Activation email is dispatched (we check the DB token, since the
     actual Resend send is integration-tested separately)
  4. POST /api/auth/login is blocked with 403 + `account_unverified`
  5. POST /api/auth/verify-email with a bad token → 400
  6. POST /api/auth/verify-email with the real token → 200, sets cookies,
     flips email_verified=true, clears the token
  7. Login works post-verification
  8. POST /api/auth/resend-activation honours the 60s cooldown
  9. POST /api/auth/resend-activation for already-verified user returns the
     same generic success (no enumeration leak)
"""
from __future__ import annotations
import os
import uuid
import time

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


@pytest.fixture
def cleanup():
    refs = {"user_ids": []}
    yield refs
    if refs["user_ids"]:
        _mongo().users.delete_many({"_id": {"$in": refs["user_ids"]}})


def _email() -> str:
    return f"iter79_{uuid.uuid4().hex[:10]}@example.com"


# ─────────────────── Terms acceptance ───────────────────
def test_register_requires_terms_agreement(cleanup):
    r = requests.post(f"{BASE_URL}/api/auth/register",
                      json={"email": _email(), "password": "password123",
                            "name": "no-terms"},  # missing terms_agreed
                      timeout=TIMEOUT)
    assert r.status_code == 400
    detail = r.json().get("detail")
    assert isinstance(detail, dict)
    assert detail.get("code") == "terms_required"


def test_register_with_terms_creates_unverified_user(cleanup):
    email = _email()
    r = requests.post(f"{BASE_URL}/api/auth/register",
                      json={"email": email, "password": "password123",
                            "terms_agreed": True},
                      timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["email_verified"] is False
    cleanup["user_ids"].append(ObjectId(body["id"]))

    db = _mongo()
    fresh = db.users.find_one({"_id": ObjectId(body["id"])})
    assert fresh["email_verified"] is False
    assert fresh.get("activation_token")
    assert fresh.get("activation_expires_at")
    assert fresh.get("accepted_terms_version")
    assert fresh.get("accepted_terms_at")


# ─────────────────── Login blocked until verified ───────────────────
def test_login_blocked_when_unverified(cleanup):
    email = _email()
    r = requests.post(f"{BASE_URL}/api/auth/register",
                      json={"email": email, "password": "password123",
                            "terms_agreed": True},
                      timeout=TIMEOUT)
    cleanup["user_ids"].append(ObjectId(r.json()["id"]))

    login = requests.post(f"{BASE_URL}/api/auth/login",
                          json={"email": email, "password": "password123"},
                          timeout=TIMEOUT)
    assert login.status_code == 403
    detail = login.json().get("detail")
    assert isinstance(detail, dict)
    assert detail.get("code") == "account_unverified"


# ─────────────────── Verify email ───────────────────
def test_verify_email_with_bad_token_400(cleanup):
    r = requests.post(f"{BASE_URL}/api/auth/verify-email",
                      json={"token": "not-a-real-token-just-padding-for-length"},
                      timeout=TIMEOUT)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_token"


def test_verify_email_happy_path(cleanup):
    email = _email()
    reg = requests.post(f"{BASE_URL}/api/auth/register",
                        json={"email": email, "password": "password123",
                              "terms_agreed": True},
                        timeout=TIMEOUT)
    uid = ObjectId(reg.json()["id"])
    cleanup["user_ids"].append(uid)

    db = _mongo()
    user = db.users.find_one({"_id": uid})
    token = user["activation_token"]

    sess = requests.Session()
    v = sess.post(f"{BASE_URL}/api/auth/verify-email",
                  json={"token": token}, timeout=TIMEOUT)
    assert v.status_code == 200, v.text
    body = v.json()
    assert body["ok"] is True
    assert body["user"]["email_verified"] is True

    # Token cleared
    fresh = db.users.find_one({"_id": uid})
    assert fresh.get("activation_token") is None
    assert fresh["email_verified"] is True
    assert fresh.get("email_verified_at")

    # Session is now authenticated — /me should return user
    me = sess.get(f"{BASE_URL}/api/auth/me", timeout=TIMEOUT)
    assert me.status_code == 200
    assert me.json()["email"] == email

    # And password-login also works
    login = requests.post(f"{BASE_URL}/api/auth/login",
                          json={"email": email, "password": "password123"},
                          timeout=TIMEOUT)
    assert login.status_code == 200


def test_verify_email_token_is_single_use(cleanup):
    email = _email()
    reg = requests.post(f"{BASE_URL}/api/auth/register",
                        json={"email": email, "password": "password123",
                              "terms_agreed": True},
                        timeout=TIMEOUT)
    uid = ObjectId(reg.json()["id"])
    cleanup["user_ids"].append(uid)
    token = _mongo().users.find_one({"_id": uid})["activation_token"]

    first = requests.post(f"{BASE_URL}/api/auth/verify-email",
                          json={"token": token}, timeout=TIMEOUT)
    assert first.status_code == 200

    second = requests.post(f"{BASE_URL}/api/auth/verify-email",
                           json={"token": token}, timeout=TIMEOUT)
    assert second.status_code == 400
    assert second.json()["detail"]["code"] == "invalid_token"


# ─────────────────── Resend ───────────────────
def test_resend_activation_generic_success_for_unknown_email(cleanup):
    r = requests.post(f"{BASE_URL}/api/auth/resend-activation",
                      json={"email": "no_such_user_iter79@example.com"},
                      timeout=TIMEOUT)
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_resend_activation_respects_cooldown(cleanup):
    email = _email()
    reg = requests.post(f"{BASE_URL}/api/auth/register",
                        json={"email": email, "password": "password123",
                              "terms_agreed": True},
                        timeout=TIMEOUT)
    cleanup["user_ids"].append(ObjectId(reg.json()["id"]))

    # Immediately requesting again — should be 429 (60s cooldown)
    r = requests.post(f"{BASE_URL}/api/auth/resend-activation",
                      json={"email": email}, timeout=TIMEOUT)
    assert r.status_code == 429
    assert r.json()["detail"]["code"] == "rate_limited"


def test_resend_activation_generates_new_token_after_cooldown(cleanup):
    email = _email()
    reg = requests.post(f"{BASE_URL}/api/auth/register",
                        json={"email": email, "password": "password123",
                              "terms_agreed": True},
                        timeout=TIMEOUT)
    uid = ObjectId(reg.json()["id"])
    cleanup["user_ids"].append(uid)
    db = _mongo()
    original_token = db.users.find_one({"_id": uid})["activation_token"]

    # Simulate cooldown elapsed by backdating activation_sent_at.
    db.users.update_one(
        {"_id": uid},
        {"$set": {"activation_sent_at": "2020-01-01T00:00:00+00:00"}},
    )
    r = requests.post(f"{BASE_URL}/api/auth/resend-activation",
                      json={"email": email}, timeout=TIMEOUT)
    assert r.status_code == 200

    new_token = db.users.find_one({"_id": uid})["activation_token"]
    assert new_token != original_token
