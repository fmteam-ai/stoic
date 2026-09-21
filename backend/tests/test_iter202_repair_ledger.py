"""iter-202 · Admin Repair Ledger (read-only view over `repair_ledger`).

  GET /api/admin/repair-ledger                     list + filters + pagination
  GET /api/admin/repair-ledger/kinds               per-kind summary
  GET /api/admin/repair-ledger/{correlation_id}    one sweep, all rows
Non-admins → 403. Live-target HTTP suite (skips without a target).
"""
import uuid
from datetime import datetime, timezone

import pytest
import requests

from live_target import require_live_base_url, resolve_admin_credentials

BASE_URL = require_live_base_url()
API = f"{BASE_URL}/api"


def _login(email, pw):
    s = requests.Session()
    r = s.post(f"{API}/auth/login", json={"email": email, "password": pw}, timeout=15)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    csrf = s.cookies.get("csrf_token")
    if csrf:
        s.headers.update({"X-CSRF-Token": csrf})
    return s


@pytest.fixture(scope="module")
def admin():
    return _login(*resolve_admin_credentials())


@pytest.fixture(scope="module")
def seeded():
    """Insert a synthetic sweep directly (the ledger is append-only, written
    by the analytics worker — there is deliberately no POST endpoint)."""
    import os
    from pymongo import MongoClient
    db = MongoClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]
    corr = f"repair_test{uuid.uuid4().hex[:8]}"
    uid = f"ledgertest_{uuid.uuid4().hex[:6]}"
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        {"correlation_id": corr, "user_id": uid, "kind": "account_dormant",
         "affected_ids": ["a1", "a2"], "count": 2, "detail": {}, "source": "health_repairs", "at": now},
        {"correlation_id": corr, "user_id": uid, "kind": "ghost_ack_old",
         "affected_ids": ["t1"], "count": 1, "detail": {}, "source": "health_repairs", "at": now},
    ]
    db.repair_ledger.insert_many(rows)
    yield {"corr": corr, "uid": uid}
    db.repair_ledger.delete_many({"correlation_id": corr})


def test_list_shape_and_filters(admin, seeded):
    r = admin.get(f"{API}/admin/repair-ledger", params={"user_id": seeded["uid"], "limit": 10}, timeout=15)
    assert r.status_code == 200, r.text
    d = r.json()
    assert set(d) >= {"rows", "total", "limit", "skip", "filters"}
    assert d["total"] == 2 and len(d["rows"]) == 2
    row = d["rows"][0]
    for k in ("id", "correlation_id", "kind", "user_id", "affected_ids", "count", "source", "at"):
        assert k in row, f"missing {k}"
    assert "_id" not in row
    assert {x["kind"] for x in d["rows"]} == {"account_dormant", "ghost_ack_old"}

    r = admin.get(f"{API}/admin/repair-ledger",
                  params={"user_id": seeded["uid"], "kind": "ghost_ack_old"}, timeout=15)
    assert r.json()["total"] == 1 and r.json()["rows"][0]["affected_ids"] == ["t1"]

    r = admin.get(f"{API}/admin/repair-ledger", params={"correlation_id": seeded["corr"]}, timeout=15)
    assert r.json()["total"] == 2


def test_pagination_and_limit_clamp(admin, seeded):
    r = admin.get(f"{API}/admin/repair-ledger",
                  params={"user_id": seeded["uid"], "limit": 1, "skip": 1}, timeout=15)
    d = r.json()
    assert d["total"] == 2 and len(d["rows"]) == 1 and d["skip"] == 1
    r = admin.get(f"{API}/admin/repair-ledger", params={"limit": 99999}, timeout=15)
    assert r.json()["limit"] == 500


def test_kinds_summary(admin, seeded):
    r = admin.get(f"{API}/admin/repair-ledger/kinds", timeout=15)
    assert r.status_code == 200, r.text
    d = r.json()
    assert set(d) >= {"kinds", "sources", "total_rows", "total_records"}
    by = {k["kind"]: k for k in d["kinds"]}
    assert by["account_dormant"]["records"] >= 2 and by["ghost_ack_old"]["sweeps"] >= 1
    assert "health_repairs" in d["sources"]
    assert d["total_rows"] >= 2 and d["total_records"] >= 3


def test_sweep_detail_groups_rows(admin, seeded):
    r = admin.get(f"{API}/admin/repair-ledger/{seeded['corr']}", timeout=15)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["correlation_id"] == seeded["corr"]
    assert d["records"] == 3 and len(d["rows"]) == 2
    assert d["user_id"] == seeded["uid"] and d["source"] == "health_repairs"
    assert admin.get(f"{API}/admin/repair-ledger/repair_nope", timeout=15).status_code == 404


def test_non_admin_forbidden():
    email = f"ledger_{uuid.uuid4().hex[:8]}@example.com"
    pw = "Ledger#Test2026!"
    r = requests.post(f"{API}/auth/register",
                      json={"email": email, "password": pw, "name": "L", "terms_agreed": True}, timeout=15)
    assert r.status_code in (200, 201), r.text
    from conftest import run_async
    from database import get_db
    run_async(get_db().users.update_one({"email": email}, {"$set": {"email_verified": True}}))
    s = _login(email, pw)
    for path in ("/admin/repair-ledger", "/admin/repair-ledger/kinds", "/admin/repair-ledger/x"):
        assert s.get(f"{API}{path}", timeout=15).status_code == 403, path
    run_async(get_db().users.delete_one({"email": email}))


def test_ledger_endpoints_are_read_only():
    import os
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "routes", "repair_ledger_routes.py")).read()
    assert "@router.post" not in src and "@router.delete" not in src
    for verb in ("insert_one", "update_one", "update_many", "delete_one", "delete_many"):
        assert verb not in src
