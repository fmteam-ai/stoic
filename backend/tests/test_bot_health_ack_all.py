"""Bot Health hard-caps + /api/ops/alerts/ack-all authz regression (iter 176)."""
import os
import time
import uuid
import datetime as dt
import requests
import pytest
from pymongo import MongoClient

BASE = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "test_database")

ADMIN_EMAIL = "admin@stoicaibot.com"
ADMIN_PWD = "admin123"


def _admin_session():
    s = requests.Session()
    r = s.post(f"{BASE}/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PWD}, timeout=30)
    assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text[:200]}"
    csrf = s.cookies.get("csrf_token") or s.cookies.get("csrf")
    if csrf:
        s.headers.update({"X-CSRF-Token": csrf})
    return s


def test_health_score_baseline():
    s = _admin_session()
    r = s.get(f"{BASE}/api/bot/health-score", timeout=30)
    assert r.status_code == 200, r.text[:300]
    d = r.json()
    print("health:", d.get("score"), d.get("status"), "hard_caps=", d.get("hard_caps"))
    assert d.get("score", 0) >= 75, f"score too low: {d}"
    assert d.get("hard_caps") in (None, []), f"unexpected hard_caps: {d.get('hard_caps')}"


def test_no_unknown_execution_intents():
    cli = MongoClient(MONGO_URL)
    db = cli[DB_NAME]
    n = db.execution_intents.count_documents({"status": "unknown"})
    assert n == 0, f"{n} execution_intents still in status=unknown"


def test_readiness_and_quick_actions_ok():
    s = _admin_session()
    r1 = s.get(f"{BASE}/api/state/readiness", timeout=30)
    r2 = s.get(f"{BASE}/api/bot/quick-actions", timeout=30)
    assert r1.status_code == 200, r1.text[:200]
    assert r2.status_code == 200, r2.text[:200]


def test_ack_all_forbidden_for_non_admin():
    s = requests.Session()
    email = f"nonadmin_{uuid.uuid4().hex[:8]}@example.com"
    reg = s.post(f"{BASE}/api/auth/register", json={
        "email": email, "password": "Passw0rd!xyz", "name": "NA", "terms_agreed": True
    }, timeout=30)
    assert reg.status_code in (200, 201), reg.text[:200]
    # flip email_verified in mongo so we can login
    cli = MongoClient(MONGO_URL)
    db = cli[DB_NAME]
    db.users.update_one({"email": email}, {"$set": {"email_verified": True}})
    lg = s.post(f"{BASE}/api/auth/login", json={"email": email, "password": "Passw0rd!xyz"}, timeout=30)
    assert lg.status_code == 200, lg.text[:300]
    csrf = s.cookies.get("csrf_token") or s.cookies.get("csrf")
    headers = {"X-CSRF-Token": csrf} if csrf else {}
    r = s.post(f"{BASE}/api/ops/alerts/ack-all", headers=headers, timeout=30)
    assert r.status_code == 403, f"expected 403 got {r.status_code}: {r.text[:200]}"
    # cleanup user
    db.users.delete_one({"email": email})


def test_ack_all_flow_admin_with_seeded_alert():
    cli = MongoClient(MONGO_URL)
    db = cli[DB_NAME]
    now = dt.datetime.utcnow().isoformat()
    seed_id = db.ops_alerts.insert_one({
        "kind": "test_seed",
        "severity": "critical",
        "message": "TEST seeded alert for ack-all verification (iter176)",
        "acked_at": None,
        "occurrences": 1,
        "first_seen": now,
        "last_seen": now,
        "synthetic": False,
    }).inserted_id
    try:
        s = _admin_session()
        # Health should now hard-cap
        r = s.get(f"{BASE}/api/bot/health-score", timeout=30)
        assert r.status_code == 200
        d = r.json()
        caps = d.get("hard_caps") or []
        codes = [c.get("code") for c in caps]
        assert "critical_alerts_open" in codes, f"expected critical_alerts_open cap, got {caps}"
        assert d.get("score", 100) <= 45, f"expected cap<=45 got {d.get('score')}"

        # ack-all
        r = s.post(f"{BASE}/api/ops/alerts/ack-all", timeout=30)
        assert r.status_code == 200, f"ack-all failed: {r.status_code} {r.text[:300]}"

        # seeded alert should now be acked
        doc = db.ops_alerts.find_one({"_id": seed_id})
        assert doc and doc.get("acked_at"), f"seeded alert not acked: {doc}"

        # health recovers
        time.sleep(1)
        r = s.get(f"{BASE}/api/bot/health-score", timeout=30)
        d = r.json()
        assert d.get("score", 0) >= 75, f"score not recovered: {d}"
        codes2 = [c.get("code") for c in (d.get("hard_caps") or [])]
        assert "critical_alerts_open" not in codes2, f"cap still present: {d.get('hard_caps')}"
    finally:
        db.ops_alerts.delete_one({"_id": seed_id})
