"""iter-145 verification: AI Value Ledger link to Risk Decision Snapshots."""
import os
import uuid
from datetime import datetime, timezone

import pytest
import requests
from bson import ObjectId
from pymongo import MongoClient

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]
ADMIN_EMAIL = "admin@stoicaibot.com"
ADMIN_PASSWORD = "admin123"


def _allow_http_cookies(s):
    orig = s.prepare_request

    def prep(req):
        for c in s.cookies:
            c.secure = False
        return orig(req)

    s.prepare_request = prep


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    if BASE_URL.startswith("http://"):
        _allow_http_cookies(s)
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
               timeout=15)
    assert r.status_code == 200, r.text
    return s


@pytest.fixture(scope="module")
def mongo():
    c = MongoClient(MONGO_URL)
    yield c[DB_NAME]
    c.close()


@pytest.fixture(scope="module")
def admin_id(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/auth/me", timeout=15)
    return r.json()["id"]


def test_pamm_risk_guard_ledger_entry_present(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/brain/value-ledger",
                          timeout=20)
    assert r.status_code == 200, r.text
    body = r.json()
    entries = body.get("entries") or []
    matches = [e for e in entries if e.get("source") == "pamm_risk_guard"]
    assert matches, f"pamm_risk_guard entry missing: {entries}"
    e = matches[0]
    assert e["effect"] == "unobservable"
    assert "n" in e
    assert "risk_snapshot_ids" in e
    assert isinstance(e["risk_snapshot_ids"], list)


def test_pamm_risk_guard_n_increments_with_seeded_reject(
        admin_session, admin_id, mongo):
    # Pick any admin-owned account
    acc = mongo.accounts.find_one({"user_id": admin_id}, {"_id": 1})
    assert acc, "admin has no accounts"
    acc_id_str = str(acc["_id"])
    now = datetime.now(timezone.utc).isoformat()
    snap_id = f"rds_iter145_{uuid.uuid4().hex[:12]}"

    # snapshot count before
    r0 = admin_session.get(f"{BASE_URL}/api/brain/value-ledger", timeout=20)
    before = next(e for e in r0.json()["entries"]
                  if e["source"] == "pamm_risk_guard")
    n_before = before["n"]

    inserted_id = None
    try:
        ins = mongo.pamm_risk_decisions.insert_one({
            "snapshot_id": snap_id,
            "account_id": acc_id_str,
            "authorized": False,
            "reason": "iter145_test",
            "at": now})
        inserted_id = ins.inserted_id

        r1 = admin_session.get(f"{BASE_URL}/api/brain/value-ledger",
                               timeout=20)
        after = next(e for e in r1.json()["entries"]
                     if e["source"] == "pamm_risk_guard")
        assert after["n"] >= n_before + 1, (before, after)
        assert snap_id in after["risk_snapshot_ids"], after
    finally:
        if inserted_id is not None:
            mongo.pamm_risk_decisions.delete_one({"_id": inserted_id})
        # confirm cleanup
        assert mongo.pamm_risk_decisions.find_one(
            {"snapshot_id": snap_id}) is None


pytestmark = pytest.mark.http
