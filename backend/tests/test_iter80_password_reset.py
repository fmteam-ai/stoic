"""iter-80 · Forgot Password / Reset Password tests.

Covers:
  - POST /api/auth/forgot-password generic-ok for unknown email
  - POST /api/auth/forgot-password 60s cooldown
  - POST /api/auth/forgot-password skipped for suspended/terminated users
    (still returns generic-ok — no enumeration leak)
  - POST /api/auth/reset-password bad token → 400
  - POST /api/auth/reset-password expired token → 400
  - POST /api/auth/reset-password happy path → password changes, token cleared
  - new password works for login, old password fails
  - single-use token
"""
from __future__ import annotations
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import os
import uuid

import pytest
import requests
from bson import ObjectId
from pymongo import MongoClient

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    try:
        with open(_os.path.join(_REPO_DIR, "frontend", ".env")) as f:
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
    with open(_os.path.join(_BACKEND_DIR, ".env")) as f:
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


def _create_verified_user(*, password="oldpass123") -> tuple[str, str, ObjectId]:
    suffix = uuid.uuid4().hex[:10]
    email = f"iter80_{suffix}@example.com"
    r = requests.post(f"{BASE_URL}/api/auth/register",
                      json={"email": email, "password": password,
                            "name": f"iter80-{suffix}", "terms_agreed": True},
                      timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    uid = ObjectId(r.json()["id"])
    _mongo().users.update_one(
        {"_id": uid},
        {"$set": {"email_verified": True},
         "$unset": {"activation_token": "", "activation_expires_at": ""}},
    )
    return email, password, uid


# ─────────────────── Forgot password ───────────────────
def test_forgot_password_generic_ok_for_unknown_email():
    r = requests.post(f"{BASE_URL}/api/auth/forgot-password",
                      json={"email": "nobody_iter80@example.com"},
                      timeout=TIMEOUT)
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_forgot_password_issues_token_for_known_user(cleanup):
    email, _, uid = _create_verified_user()
    cleanup["user_ids"].append(uid)

    r = requests.post(f"{BASE_URL}/api/auth/forgot-password",
                      json={"email": email}, timeout=TIMEOUT)
    assert r.status_code == 200

    fresh = _mongo().users.find_one({"_id": uid})
    assert fresh.get("password_reset_token")
    assert fresh.get("password_reset_expires_at")


def test_forgot_password_respects_cooldown(cleanup):
    email, _, uid = _create_verified_user()
    cleanup["user_ids"].append(uid)

    requests.post(f"{BASE_URL}/api/auth/forgot-password",
                  json={"email": email}, timeout=TIMEOUT)
    # Immediate retry → 429
    r2 = requests.post(f"{BASE_URL}/api/auth/forgot-password",
                       json={"email": email}, timeout=TIMEOUT)
    assert r2.status_code == 429
    assert r2.json()["detail"]["code"] == "rate_limited"


def test_forgot_password_skips_suspended_users_silently(cleanup):
    email, _, uid = _create_verified_user()
    cleanup["user_ids"].append(uid)
    _mongo().users.update_one(
        {"_id": uid},
        {"$set": {"status": "suspended", "suspension_reason": "test"}},
    )
    r = requests.post(f"{BASE_URL}/api/auth/forgot-password",
                      json={"email": email}, timeout=TIMEOUT)
    # Generic OK, no token issued
    assert r.status_code == 200
    fresh = _mongo().users.find_one({"_id": uid})
    assert not fresh.get("password_reset_token")


# ─────────────────── Reset password ───────────────────
def test_reset_password_bad_token_400():
    r = requests.post(f"{BASE_URL}/api/auth/reset-password",
                      json={"token": "not-a-real-token-padding-1234567",
                            "new_password": "newpass123"},
                      timeout=TIMEOUT)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_token"


def test_reset_password_expired_token_400(cleanup):
    email, _, uid = _create_verified_user()
    cleanup["user_ids"].append(uid)
    # Plant an expired token directly.
    expired_token = "expired_iter80_" + uuid.uuid4().hex
    _mongo().users.update_one(
        {"_id": uid},
        {"$set": {
            "password_reset_token": expired_token,
            "password_reset_expires_at": "2020-01-01T00:00:00+00:00",
            "password_reset_sent_at": "2020-01-01T00:00:00+00:00",
        }},
    )
    r = requests.post(f"{BASE_URL}/api/auth/reset-password",
                      json={"token": expired_token, "new_password": "newpass123"},
                      timeout=TIMEOUT)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "expired_token"


def test_reset_password_happy_path_changes_password(cleanup):
    email, old_password, uid = _create_verified_user(password="oldpassword")
    cleanup["user_ids"].append(uid)

    # Issue token
    requests.post(f"{BASE_URL}/api/auth/forgot-password",
                  json={"email": email}, timeout=TIMEOUT)
    token = _mongo().users.find_one({"_id": uid})["password_reset_token"]

    # Reset
    r = requests.post(f"{BASE_URL}/api/auth/reset-password",
                      json={"token": token, "new_password": "newpassword"},
                      timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    # Token is cleared
    fresh = _mongo().users.find_one({"_id": uid})
    assert fresh.get("password_reset_token") is None
    assert fresh.get("password_reset_at")

    # Old password no longer works
    login_old = requests.post(f"{BASE_URL}/api/auth/login",
                              json={"email": email, "password": old_password},
                              timeout=TIMEOUT)
    assert login_old.status_code == 401

    # New password works
    login_new = requests.post(f"{BASE_URL}/api/auth/login",
                              json={"email": email, "password": "newpassword"},
                              timeout=TIMEOUT)
    assert login_new.status_code == 200


def test_reset_password_token_is_single_use(cleanup):
    email, _, uid = _create_verified_user()
    cleanup["user_ids"].append(uid)
    requests.post(f"{BASE_URL}/api/auth/forgot-password",
                  json={"email": email}, timeout=TIMEOUT)
    token = _mongo().users.find_one({"_id": uid})["password_reset_token"]

    first = requests.post(f"{BASE_URL}/api/auth/reset-password",
                          json={"token": token, "new_password": "newpassword"},
                          timeout=TIMEOUT)
    assert first.status_code == 200

    second = requests.post(f"{BASE_URL}/api/auth/reset-password",
                           json={"token": token, "new_password": "another1"},
                           timeout=TIMEOUT)
    assert second.status_code == 400
    assert second.json()["detail"]["code"] == "invalid_token"


def test_reset_password_min_length_enforced(cleanup):
    email, _, uid = _create_verified_user()
    cleanup["user_ids"].append(uid)
    requests.post(f"{BASE_URL}/api/auth/forgot-password",
                  json={"email": email}, timeout=TIMEOUT)
    token = _mongo().users.find_one({"_id": uid})["password_reset_token"]

    r = requests.post(f"{BASE_URL}/api/auth/reset-password",
                      json={"token": token, "new_password": "abc"},
                      timeout=TIMEOUT)
    assert r.status_code == 422  # Pydantic min_length=6 violation
