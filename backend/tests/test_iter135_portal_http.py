"""iter-135 HTTP/e2e tests: public status, legal, support tickets, onboarding.

Exercises the actual HTTP surface (with CSRF double-submit on cookie-auth)
via REACT_APP_BACKEND_URL so we mirror what the browser does.
"""
import os
import sys
import time
import uuid

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import base_url, mark_email_verified, mongo_db  # noqa: E402

API = f"{base_url()}/api"
PW = "Kd5#Zt9mW2xVpR7c"


def _session_with_csrf(email, password, name="QA"):
    s = requests.Session()
    r = s.post(f"{API}/auth/register",
               json={"email": email, "password": password, "name": name,
                     "terms_agreed": True}, timeout=30)
    assert r.status_code == 200, f"register: {r.status_code} {r.text}"
    mark_email_verified(email)
    r = s.post(f"{API}/auth/login",
               json={"email": email, "password": password}, timeout=30)
    assert r.status_code == 200, f"login: {r.status_code} {r.text}"
    csrf = s.cookies.get("csrf_token")
    assert csrf, "csrf cookie missing"
    s.headers.update({"X-CSRF-Token": csrf})
    return s


def _login(email, password):
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": email, "password": password}, timeout=30)
    assert r.status_code == 200, f"login: {r.status_code} {r.text}"
    csrf = s.cookies.get("csrf_token")
    s.headers.update({"X-CSRF-Token": csrf})
    return s


# -------------------------------------------------------------- public

def test_public_status_endpoint():
    r = requests.get(f"{API}/status", timeout=30)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["overall"] in ("operational", "degraded", "major_outage")
    for k in ("api", "database", "bot_engine", "ea_bridge",
              "payments", "email"):
        assert k in body["components"], f"missing {k}"
    assert body["components"]["database"]["status"] == "operational"
    assert body["components"]["api"]["status"] == "operational"


@pytest.mark.parametrize("kind,marker", [
    ("privacy", "Privacy Policy"),
    ("risk", "Risk Disclosure"),
])
def test_legal_docs_public(kind, marker):
    r = requests.get(f"{API}/legal/{kind}", timeout=30)
    assert r.status_code == 200, r.text
    body = r.json()
    assert marker in body["markdown"]
    assert body["version"]
    assert body["kind"] == kind


def test_legal_unknown_404():
    r = requests.get(f"{API}/legal/bogus", timeout=30)
    assert r.status_code == 404


# -------------------------------------------------------------- support tickets

@pytest.fixture(scope="module")
def owner_session():
    email = f"TEST-owner-{uuid.uuid4().hex[:10]}@example.com"
    s = _session_with_csrf(email, PW)
    s._email = email
    return s


@pytest.fixture(scope="module")
def other_session():
    email = f"TEST-other-{uuid.uuid4().hex[:10]}@example.com"
    s = _session_with_csrf(email, PW)
    s._email = email
    return s


@pytest.fixture(scope="module")
def admin_session():
    return _login("admin@stoicaibot.com", "admin123")


def test_ticket_create_and_get(owner_session):
    r = owner_session.post(f"{API}/support/tickets",
                           json={"category": "billing",
                                 "subject": "Please refund my plan",
                                 "message": "I need a refund for annual."},
                           timeout=30)
    assert r.status_code == 200, r.text
    t = r.json()
    assert t["status"] == "open"
    assert t["category"] == "billing"
    assert t["message_count"] == 1
    tid = t["id"]
    owner_session._tid = tid

    # my list contains it
    r = owner_session.get(f"{API}/support/tickets", timeout=30)
    assert r.status_code == 200
    assert any(x["id"] == tid for x in r.json())

    # fetch full thread
    r = owner_session.get(f"{API}/support/tickets/{tid}", timeout=30)
    assert r.status_code == 200
    body = r.json()
    assert body["messages"][0]["by"] == "user"


def test_ticket_validation(owner_session):
    r = owner_session.post(f"{API}/support/tickets",
                           json={"category": "bogus", "subject": "hey",
                                 "message": "long enough message"}, timeout=30)
    assert r.status_code == 400
    r = owner_session.post(f"{API}/support/tickets",
                           json={"category": "billing", "subject": "ab",
                                 "message": "short"}, timeout=30)
    assert r.status_code == 400


def test_ticket_idor_other_user_gets_404(owner_session, other_session):
    tid = owner_session._tid
    r = other_session.get(f"{API}/support/tickets/{tid}", timeout=30)
    assert r.status_code == 404


def test_admin_queue_and_reply(owner_session, admin_session):
    tid = owner_session._tid

    # non-admin cannot access admin queue
    r = owner_session.get(f"{API}/support/admin/tickets?status=open", timeout=30)
    assert r.status_code == 403

    # admin sees queue with counts
    r = admin_session.get(f"{API}/support/admin/tickets?status=open", timeout=30)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "counts" in body and set(body["counts"].keys()) >= {"open", "answered", "closed"}
    assert any(t["id"] == tid for t in body["tickets"])

    # admin replies
    r = admin_session.post(f"{API}/support/tickets/{tid}/reply",
                           json={"message": "We have processed your refund."},
                           timeout=30)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "answered"
    assert body["last_reply_by"] == "admin"
    assert body["message_count"] == 2

    # user sees it as answered
    r = owner_session.get(f"{API}/support/tickets/{tid}", timeout=30)
    assert r.json()["status"] == "answered"


def test_ticket_user_reply_reopens(owner_session):
    tid = owner_session._tid
    r = owner_session.post(f"{API}/support/tickets/{tid}/reply",
                           json={"message": "Thank you!"}, timeout=30)
    assert r.status_code == 200
    assert r.json()["status"] == "open"


def test_ticket_close_blocks_reply(owner_session):
    tid = owner_session._tid
    r = owner_session.post(f"{API}/support/tickets/{tid}/close", timeout=30)
    assert r.status_code == 200
    r = owner_session.post(f"{API}/support/tickets/{tid}/reply",
                           json={"message": "another"}, timeout=30)
    assert r.status_code == 400


# -------------------------------------------------------------- onboarding

def test_onboarding_new_user_pending():
    email = f"TEST-onb-{uuid.uuid4().hex[:10]}@example.com"
    s = _session_with_csrf(email, PW)
    r = s.get(f"{API}/onboarding", timeout=30)
    assert r.status_code == 200, r.text
    ob = r.json()
    assert ob["status"] == "pending"
    assert ob["step"] == 0

    # persist step + risk
    r = s.put(f"{API}/onboarding",
              json={"step": 2, "risk_level": "high"}, timeout=30)
    assert r.status_code == 200, r.text
    ob = r.json()
    assert ob["step"] == 2 and ob["risk_level"] == "high"

    # resumes across a fresh state fetch
    r = s.get(f"{API}/onboarding", timeout=30)
    assert r.json()["step"] == 2

    # skip -> hidden
    r = s.put(f"{API}/onboarding", json={"status": "skipped"}, timeout=30)
    assert r.json()["status"] == "skipped"

    # invalid rejected
    r = s.put(f"{API}/onboarding", json={"status": "weird"}, timeout=30)
    assert r.status_code == 400
    r = s.put(f"{API}/onboarding", json={"risk_level": "extreme"}, timeout=30)
    assert r.status_code == 400


def test_onboarding_existing_trader_auto_done():
    """User with an accounts doc must auto-flip to done."""
    email = f"TEST-trader-{uuid.uuid4().hex[:10]}@example.com"
    s = _session_with_csrf(email, PW)
    # seed an accounts doc for this user
    db = mongo_db()
    u = db.users.find_one({"email": email.lower()})
    db.accounts.insert_one({"user_id": str(u["_id"]),
                            "display_name": "TEST_seed"})
    # ensure no onboarding yet
    db.users.update_one({"_id": u["_id"]}, {"$unset": {"onboarding": ""}})
    r = s.get(f"{API}/onboarding", timeout=30)
    assert r.status_code == 200
    assert r.json()["status"] == "done"
    db.accounts.delete_many({"user_id": str(u["_id"])})
