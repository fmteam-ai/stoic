"""iter-148 · Live E2E for EA v1.52 bridge/report + register password policy + execution-health.
Seeds one trade + one account bridge_token directly in MongoDB, POSTs a v1.52-style report,
verifies mutation, and confirms the duplicate-ticket guard tolerates order_ticket==stored mt5.
"""
import os, sys, asyncio, time
import requests
from bson import ObjectId
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_DIR = os.path.dirname(_TESTS_DIR)
_REPO_DIR = os.path.dirname(_BACKEND_DIR)
sys.path.insert(0, os.environ.get("BACKEND_DIR", _BACKEND_DIR))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.environ.get("BACKEND_DIR", _BACKEND_DIR), ".env"))
from motor.motor_asyncio import AsyncIOMotorClient
from live_target import require_live_base_url

BASE = require_live_base_url()
MARK = "iter148_p0_live"


def _db():
    cli = AsyncIOMotorClient(os.environ["MONGO_URL"])
    return cli[os.environ["DB_NAME"]]


def test_health_ready():
    r = requests.get(f"{BASE}/api/health/ready", timeout=15)
    assert r.status_code == 200, r.text


def _login():
    s = requests.Session()
    r = s.post(f"{BASE}/api/auth/login",
               json={"email": "admin@trading.bot", "password": "admin123"},
               timeout=15)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return s


def test_login_admin_8char_still_works():
    _login()


def test_register_password_min_length_8():
    email = f"iter148_pw_{int(time.time()*1000)}@t.example"
    # 7 chars -> 422
    r = requests.post(f"{BASE}/api/auth/register",
                      json={"email": email, "password": "abc1234",
                            "terms_agreed": True}, timeout=15)
    assert r.status_code == 422, f"expected 422 for 7-char pw, got {r.status_code} {r.text}"
    # 8 chars -> 200/201
    r2 = requests.post(f"{BASE}/api/auth/register",
                       json={"email": email, "password": "Nw3#Xr8vB5tYqJ6u",
                             "terms_agreed": True}, timeout=15)
    assert r2.status_code in (200, 201), f"8-char register failed: {r2.status_code} {r2.text}"
    # cleanup
    async def _clean():
        d = _db()
        await d.users.delete_one({"email": email})
    asyncio.run(_clean())


def test_execution_health_shape():
    s = _login()
    r = s.get(f"{BASE}/api/bot/execution-health", timeout=20)
    assert r.status_code == 200, r.text
    j = r.json()
    for key in ("outbox", "workers", "account_leases", "lifecycle",
                "protection", "costs"):
        assert key in j, f"execution-health missing {key}: keys={list(j.keys())}"


def test_bridge_report_v152_partial_fill_and_ticket_separation():
    async def _run():
        d = _db()
        acc = await d.accounts.find_one({"bridge_token": {"$exists": True, "$ne": None}})
        assert acc, "need at least one account with a bridge_token seeded"
        bridge_token = acc["bridge_token"]
        account_id = str(acc["_id"])
        trade_doc = {
            "account_id": account_id,
            "symbol": "EURUSD",
            "action": "BUY",
            "lot_size": 1.00,
            "entry_price": 1.10000,
            "status": "queued",
            "marker": MARK,
        }
        ins = await d.trades.insert_one(trade_doc)
        trade_id = str(ins.inserted_id)
        try:
            payload = {
                "bridge_token": bridge_token,
                "trade_id": trade_id,
                "mt5_ticket": 9200000002,     # position id
                "status": "open",
                "entry_price": 1.10005,
                "order_ticket": 9100000001,
                "deal_ticket": 9150000001,
                "position_id": 9200000002,
                "filled_volume": 0.30,        # < 1.00 requested
                "partial_fill": True,
            }
            r = requests.post(f"{BASE}/api/bridge/report", json=payload, timeout=20)
            assert r.status_code == 200, f"report failed: {r.status_code} {r.text}"
            body = r.json()
            assert body.get("ok") is True, body
            assert not body.get("duplicate"), f"first report incorrectly flagged dup: {body}"
            got = await d.trades.find_one({"_id": ins.inserted_id})
            assert got["status"] == "open"
            assert got["mt5_ticket"] == 9200000002
            assert got["order_ticket"] == 9100000001
            assert got["deal_ticket"] == 9150000001
            assert got["position_id"] == 9200000002
            assert got["partial_fill"] is True
            assert abs(got["original_lot_size"] - 1.00) < 1e-9
            assert abs(got["lot_size"] - 0.30) < 1e-9
            print("Step-1 open partial-fill mutation OK:", {
                k: got.get(k) for k in ("mt5_ticket", "order_ticket",
                                        "deal_ticket", "position_id",
                                        "partial_fill", "lot_size",
                                        "original_lot_size")})

            # Duplicate guard: a replay whose `order_ticket` equals the
            # stored mt5_ticket (pre-1.52 legacy path) must NOT be flagged
            # duplicate even if payload.mt5_ticket (position id) differs.
            # Seed a legacy trade whose mt5_ticket == order_ticket.
            legacy_doc = {k: v for k, v in trade_doc.items() if k != "_id"}
            legacy_doc["mt5_ticket"] = 8100000001  # legacy stored as order ticket
            ins2 = await d.trades.insert_one(legacy_doc)
            try:
                payload2 = {
                    "bridge_token": bridge_token,
                    "trade_id": str(ins2.inserted_id),
                    "mt5_ticket": 8200000001,       # now the position id
                    "status": "open",
                    "entry_price": 1.10005,
                    "order_ticket": 8100000001,     # same as stored legacy
                    "deal_ticket": 8150000001,
                    "position_id": 8200000001,
                }
                r2 = requests.post(f"{BASE}/api/bridge/report", json=payload2, timeout=20)
                assert r2.status_code == 200, r2.text
                body2 = r2.json()
                assert not body2.get("duplicate"), \
                    f"legacy order_ticket==stored mt5 must NOT be flagged duplicate: {body2}"
                got2 = await d.trades.find_one({"_id": ins2.inserted_id})
                # position id should now overwrite mt5_ticket via the update path
                assert got2["mt5_ticket"] == 8200000001, got2.get("mt5_ticket")
                assert got2["order_ticket"] == 8100000001
                print("Step-2 duplicate-guard tolerates order->position transition OK")
            finally:
                await d.trades.delete_one({"_id": ins2.inserted_id})

            # Real duplicate: mt5_ticket differs AND order_ticket differs -> flagged
            ins3 = await d.trades.insert_one({
                **{k: v for k, v in trade_doc.items() if k != "_id"},
                "mt5_ticket": 7100000001})
            try:
                payload3 = {
                    "bridge_token": bridge_token,
                    "trade_id": str(ins3.inserted_id),
                    "mt5_ticket": 7999999999,
                    "status": "open",
                    "entry_price": 1.10005,
                    "order_ticket": 7999999998,
                }
                r3 = requests.post(f"{BASE}/api/bridge/report", json=payload3, timeout=20)
                assert r3.status_code == 200
                b3 = r3.json()
                assert b3.get("duplicate") is True, f"true dup must be flagged: {b3}"
                got3 = await d.trades.find_one({"_id": ins3.inserted_id})
                assert got3.get("requires_reconciliation") is True
                print("Step-3 real-duplicate correctly flagged")
            finally:
                await d.trades.delete_one({"_id": ins3.inserted_id})
        finally:
            await d.trades.delete_one({"_id": ins.inserted_id})
    asyncio.run(_run())


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
