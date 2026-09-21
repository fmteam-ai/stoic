"""iter-202 · Admin Repair Ledger — live HTTP tests against preview,
using direct motor client (bypasses shared-loop conftest fixture issues)."""
import asyncio
import os
import uuid
from datetime import datetime, timezone

import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL") or os.environ.get("BACKEND_URL")
if not BASE_URL:
    # Try to read frontend/.env
    try:
        with open("/app/frontend/.env") as f:
            for line in f:
                if line.startswith("REACT_APP_BACKEND_URL="):
                    BASE_URL = line.split("=", 1)[1].strip().strip('"')
                    break
    except Exception:
        pass
BASE_URL = (BASE_URL or "").rstrip("/")
API = f"{BASE_URL}/api"

ADMIN_EMAIL = "admin@stoicaibot.com"
ADMIN_PW = "admin123"

# Read Mongo config from backend/.env
def _read_env():
    env = {}
    try:
        with open("/app/backend/.env") as f:
            for line in f:
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.strip().split("=", 1)
                    env[k] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return env

_ENV = _read_env()
MONGO_URL = _ENV.get("MONGO_URL") or os.environ.get("MONGO_URL")
DB_NAME = _ENV.get("DB_NAME") or os.environ.get("DB_NAME")


def _login(email, pw):
    s = requests.Session()
    r = s.post(f"{API}/auth/login", json={"email": email, "password": pw}, timeout=15)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    csrf = s.cookies.get("csrf_token")
    if csrf:
        s.headers.update({"X-CSRF-Token": csrf})
    return s


async def _seed_and_run(corr, uid, rows):
    from motor.motor_asyncio import AsyncIOMotorClient
    cli = AsyncIOMotorClient(MONGO_URL)
    db = cli[DB_NAME]
    await db.repair_ledger.insert_many(rows)
    cli.close()


async def _cleanup(corr):
    from motor.motor_asyncio import AsyncIOMotorClient
    cli = AsyncIOMotorClient(MONGO_URL)
    db = cli[DB_NAME]
    await db.repair_ledger.delete_many({"correlation_id": corr})
    cli.close()


async def _count():
    from motor.motor_asyncio import AsyncIOMotorClient
    cli = AsyncIOMotorClient(MONGO_URL)
    db = cli[DB_NAME]
    n = await db.repair_ledger.count_documents({})
    cli.close()
    return n


async def _flip_verified(email):
    from motor.motor_asyncio import AsyncIOMotorClient
    cli = AsyncIOMotorClient(MONGO_URL)
    db = cli[DB_NAME]
    await db.users.update_one({"email": email}, {"$set": {"email_verified": True}})
    cli.close()


async def _delete_user(email):
    from motor.motor_asyncio import AsyncIOMotorClient
    cli = AsyncIOMotorClient(MONGO_URL)
    db = cli[DB_NAME]
    await db.users.delete_one({"email": email})
    cli.close()


@pytest.fixture(scope="module")
def admin():
    return _login(ADMIN_EMAIL, ADMIN_PW)


@pytest.fixture(scope="module")
def seeded():
    corr = f"repair_test{uuid.uuid4().hex[:8]}"
    uid = f"ledgertest_{uuid.uuid4().hex[:6]}"
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        {"correlation_id": corr, "user_id": uid, "kind": "account_dormant",
         "affected_ids": ["a1", "a2"], "count": 2, "detail": {}, "source": "health_repairs", "at": now},
        {"correlation_id": corr, "user_id": uid, "kind": "ghost_ack_old",
         "affected_ids": ["t1"], "count": 1, "detail": {}, "source": "health_repairs", "at": now},
    ]
    asyncio.run(_seed_and_run(corr, uid, rows))
    yield {"corr": corr, "uid": uid}
    asyncio.run(_cleanup(corr))


def test_list_shape_and_filters(admin, seeded):
    r = admin.get(f"{API}/admin/repair-ledger", params={"user_id": seeded["uid"], "limit": 10}, timeout=15)
    assert r.status_code == 200, r.text
    d = r.json()
    assert set(d) >= {"rows", "total", "limit", "skip", "filters"}
    assert d["total"] == 2 and len(d["rows"]) == 2
    row = d["rows"][0]
    for k in ("id", "correlation_id", "kind", "user_id", "affected_ids", "count", "source", "at"):
        assert k in row
    assert "_id" not in row
    assert {x["kind"] for x in d["rows"]} == {"account_dormant", "ghost_ack_old"}

    r = admin.get(f"{API}/admin/repair-ledger",
                  params={"user_id": seeded["uid"], "kind": "ghost_ack_old"}, timeout=15)
    assert r.json()["total"] == 1 and r.json()["rows"][0]["affected_ids"] == ["t1"]

    r = admin.get(f"{API}/admin/repair-ledger", params={"correlation_id": seeded["corr"]}, timeout=15)
    assert r.json()["total"] == 2

    r = admin.get(f"{API}/admin/repair-ledger",
                  params={"source": "health_repairs", "user_id": seeded["uid"]}, timeout=15)
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
    asyncio.run(_flip_verified(email))
    s = _login(email, pw)
    for path in ("/admin/repair-ledger", "/admin/repair-ledger/kinds", "/admin/repair-ledger/x"):
        assert s.get(f"{API}{path}", timeout=15).status_code == 403, path
    asyncio.run(_delete_user(email))


def test_ledger_endpoints_are_read_only(admin):
    n_before = asyncio.run(_count())
    admin.get(f"{API}/admin/repair-ledger", timeout=15)
    admin.get(f"{API}/admin/repair-ledger/kinds", timeout=15)
    admin.get(f"{API}/admin/repair-ledger/repair_nope", timeout=15)
    n_after = asyncio.run(_count())
    assert n_before == n_after, f"ledger row count changed: {n_before} → {n_after}"
