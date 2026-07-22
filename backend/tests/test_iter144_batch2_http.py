"""iter-144 · Architectural hardening Batch 2 (audit r3) — HTTP/live verification.

Covers 5 items requested by the review:
  1. POST /api/bridge/modification-ack fencing (stale/current/replay/legacy)
  2. Lifecycle quarantine via scalp.order_state.apply() (live motor DB)
  3. Unified reservation release via risk_reservations.release_for_trade
  4. GET /api/health/ready — startup key ABSENT + status 200
  5. Regression smoke — admin login, bridge heartbeat 200

Uses the live process (REACT_APP_BACKEND_URL) for HTTP and a direct motor
connection to MONGO_URL for DB-level assertions / seed data. Every seed row
is prefixed with a marker and cleaned up at teardown.
"""
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import asyncio
import os
import sys
import time
import uuid
from datetime import datetime, timezone

import pytest
import requests
from bson import ObjectId

# Make the backend package importable for scalp.order_state / risk_reservations
sys.path.insert(0, _BACKEND_DIR)

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    with open(_os.path.join(_REPO_DIR, "frontend", ".env")) as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = line.split("=", 1)[1].strip().rstrip("/")
                break

# Explicitly load backend/.env for MONGO_URL / DB_NAME (pytest doesn't source it)
from dotenv import load_dotenv
load_dotenv(_os.path.join(_BACKEND_DIR, ".env"))

MONGO_URL = os.environ.get("MONGO_URL")
DB_NAME = os.environ.get("DB_NAME")
TEST_MARKER = "iter144_batch2_http"


# --------------------------------------------------------------- helpers
def _admin_session() -> requests.Session:
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": "admin@trading.bot", "password": "admin123"},
               timeout=15)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    csrf = s.cookies.get("csrf_token")
    if csrf:
        s.headers.update({"X-CSRF-Token": csrf})
    return s


def _get_motor_db():
    from motor.motor_asyncio import AsyncIOMotorClient
    client = AsyncIOMotorClient(MONGO_URL)
    return client, client[DB_NAME]


# --------------------------------------------------------------- fixtures
@pytest.fixture(scope="module")
def admin():
    return _admin_session()


@pytest.fixture(scope="module")
def admin_account(admin):
    """Return dict of the first admin-owned account with a bridge_token."""
    r = admin.get(f"{BASE_URL}/api/accounts", timeout=15)
    assert r.status_code == 200, f"GET /api/accounts failed: {r.status_code} {r.text}"
    accs = r.json()
    for a in accs:
        if a.get("bridge_token"):
            return a
    pytest.skip("no admin-owned account with bridge_token")


@pytest.fixture(scope="module")
def bridge_token(admin_account):
    return admin_account["bridge_token"]


@pytest.fixture(scope="module")
def account_id(admin_account):
    return admin_account["id"] if "id" in admin_account else admin_account.get("_id")


@pytest.fixture(scope="module", autouse=True)
def _cleanup(admin_account):
    yield
    async def _run():
        client, db = _get_motor_db()
        try:
            await db.trades.delete_many({"marker": TEST_MARKER})
            await db.risk_reservations.delete_many({"marker": TEST_MARKER})
        finally:
            client.close()
    try:
        asyncio.get_event_loop().run_until_complete(_run())
    except RuntimeError:
        asyncio.new_event_loop().run_until_complete(_run())


# =============================================================== item 4
class TestHealthReadyStartupKeyAbsent:
    def test_ready_200_and_no_startup_failure(self, admin):
        r = requests.get(f"{BASE_URL}/api/health/ready", timeout=15)
        assert r.status_code == 200, f"ready endpoint 503: {r.text}"
        body = r.json()
        assert body.get("status") == "ok"
        checks = body.get("checks") or {}
        # 'startup' key MUST be absent when startup succeeded
        assert "startup" not in checks, (
            f"startup key should be absent on success, got: {checks}")
        assert checks.get("db") == "ok"


# =============================================================== item 5
class TestRegressionSmoke:
    def test_admin_login_works(self):
        s = requests.Session()
        r = s.post(f"{BASE_URL}/api/auth/login",
                   json={"email": "admin@trading.bot", "password": "admin123"},
                   timeout=15)
        assert r.status_code == 200, r.text
        data = r.json()
        # response echoes user identity — accept either flat or nested shape
        email = data.get("email") or (data.get("user") or {}).get("email")
        assert email == "admin@trading.bot", data

    def test_bridge_heartbeat_valid_token_200(self, bridge_token):
        r = requests.post(
            f"{BASE_URL}/api/bridge/heartbeat",
            json={"bridge_token": bridge_token,
                  "balance": 10000.0, "equity": 10000.0,
                  "open_positions": 0, "open_tickets": []},
            timeout=15)
        assert r.status_code == 200, f"heartbeat failed: {r.status_code} {r.text}"

    def test_bridge_heartbeat_bad_token_401(self):
        r = requests.post(
            f"{BASE_URL}/api/bridge/heartbeat",
            json={"bridge_token": "not-a-real-token",
                  "balance": 0, "equity": 0, "open_positions": 0},
            timeout=15)
        assert r.status_code == 401, r.text

    def test_bridge_poll_trades_valid_token(self, bridge_token):
        # poll-trades is a normal EA long-poll — accept 200 with a list
        r = requests.post(
            f"{BASE_URL}/api/bridge/poll-trades",
            json={"bridge_token": bridge_token},
            timeout=15)
        assert r.status_code == 200, f"poll-trades failed: {r.status_code} {r.text}"


# =============================================================== item 1
# modification-ack fencing
class TestModificationAckFencing:
    def _seed_trade(self, account_id: str, intent_id: str) -> str:
        """Seed a trade doc directly in Mongo owned by the admin account
        with a pending_modification carrying intent_id and status=open."""
        async def _run():
            client, db = _get_motor_db()
            try:
                oid = ObjectId()
                doc = {
                    "_id": oid,
                    "marker": TEST_MARKER,
                    "account_id": str(account_id),
                    "mt5_ticket": int(time.time() * 1000) % 2_000_000_000,
                    "symbol": "XAUUSD",
                    "action": "BUY",
                    "entry_price": 2000.0,
                    "stop_loss": 1990.0,
                    "lot_size": 0.10,
                    "status": "open",
                    "scope": "long",                 # NOT scalp_fast
                    "pending_modification": {
                        "type": "MODIFY_SL",
                        "new_sl": 1995.0,
                        "intent_id": intent_id,
                        "seq": 1,
                        "requested_at": datetime.now(timezone.utc).isoformat(),
                    },
                    "executed_intents": [],
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "lifecycle_state": "OPEN",
                    "lifecycle_version": 1,
                }
                await db.trades.insert_one(doc)
                return str(oid)
            finally:
                client.close()
        try:
            loop = asyncio.new_event_loop()
            return loop.run_until_complete(_run())
        finally:
            loop.close()

    def _fetch_trade(self, trade_id: str) -> dict:
        async def _run():
            client, db = _get_motor_db()
            try:
                return await db.trades.find_one({"_id": ObjectId(trade_id)})
            finally:
                client.close()
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_run())
        finally:
            loop.close()

    def test_stale_intent_ack_ignored_and_sl_unchanged(
            self, admin, bridge_token, account_id):
        cur_intent = uuid.uuid4().hex
        tid = self._seed_trade(account_id, cur_intent)
        try:
            r = requests.post(
                f"{BASE_URL}/api/bridge/modification-ack",
                json={"bridge_token": bridge_token, "trade_id": tid,
                      "type": "MODIFY_SL", "success": True,
                      "new_sl": 1998.5, "intent_id": "STALE_OLD"},
                timeout=15)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body.get("status") == "stale_intent_ignored", body
            doc = self._fetch_trade(tid)
            assert doc is not None
            assert doc.get("stop_loss") == 1990.0, (
                f"stop_loss should be unchanged, got {doc.get('stop_loss')}")
            # pending_modification is still there with the current intent
            pm = doc.get("pending_modification") or {}
            assert pm.get("intent_id") == cur_intent, pm
            # executed_intents did NOT grow
            assert "STALE_OLD" not in (doc.get("executed_intents") or [])
        finally:
            self._delete(tid)

    def test_current_intent_ack_applies_and_pushes_executed(
            self, admin, bridge_token, account_id):
        cur_intent = uuid.uuid4().hex
        tid = self._seed_trade(account_id, cur_intent)
        try:
            r = requests.post(
                f"{BASE_URL}/api/bridge/modification-ack",
                json={"bridge_token": bridge_token, "trade_id": tid,
                      "type": "MODIFY_SL", "success": True,
                      "new_sl": 1998.5, "intent_id": cur_intent},
                timeout=15)
            assert r.status_code == 200, r.text
            body = r.json()
            # NOT stale — should not be a stale response
            assert body.get("status") != "stale_intent_ignored", body
            doc = self._fetch_trade(tid)
            assert doc is not None
            assert doc.get("stop_loss") == 1998.5, (
                f"expected stop_loss=1998.5 after apply, got {doc.get('stop_loss')}")
            assert doc.get("pending_modification") is None, (
                f"pending_modification should be cleared, got "
                f"{doc.get('pending_modification')}")
            executed = doc.get("executed_intents") or []
            assert cur_intent in executed, (
                f"intent_id should be pushed to executed_intents, got {executed}")

            # (c) replay the SAME ack — pending is cleared, but intent is in
            # executed_intents → must be stale.
            r2 = requests.post(
                f"{BASE_URL}/api/bridge/modification-ack",
                json={"bridge_token": bridge_token, "trade_id": tid,
                      "type": "MODIFY_SL", "success": True,
                      "new_sl": 1500.0,      # try to move SL far — must be refused
                      "intent_id": cur_intent},
                timeout=15)
            assert r2.status_code == 200, r2.text
            assert r2.json().get("status") == "stale_intent_ignored", r2.json()
            doc2 = self._fetch_trade(tid)
            assert doc2.get("stop_loss") == 1998.5, (
                f"replay should not move SL, got {doc2.get('stop_loss')}")
        finally:
            self._delete(tid)

    def test_ack_without_intent_id_processes_legacy_compat(
            self, admin, bridge_token, account_id):
        # No intent_id in payload → pre-v1.49 EA compat, processes normally
        # even though the trade has a pending_modification with an intent.
        cur_intent = uuid.uuid4().hex
        tid = self._seed_trade(account_id, cur_intent)
        try:
            r = requests.post(
                f"{BASE_URL}/api/bridge/modification-ack",
                json={"bridge_token": bridge_token, "trade_id": tid,
                      "type": "MODIFY_SL", "success": True,
                      "new_sl": 1997.0},
                timeout=15)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body.get("status") != "stale_intent_ignored", body
            doc = self._fetch_trade(tid)
            assert doc.get("stop_loss") == 1997.0, (
                f"legacy ack should have applied SL, got {doc.get('stop_loss')}")
            assert doc.get("pending_modification") is None
            # executed_intents unchanged (no intent_id in payload → not pushed)
            assert cur_intent not in (doc.get("executed_intents") or [])
        finally:
            self._delete(tid)

    def _delete(self, trade_id: str):
        async def _run():
            client, db = _get_motor_db()
            try:
                await db.trades.delete_one({"_id": ObjectId(trade_id)})
            finally:
                client.close()
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_run())
        finally:
            loop.close()


# =============================================================== item 2
# Lifecycle quarantine via scalp.order_state.apply()
class TestLifecycleQuarantineLive:
    def _run_async(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_new_stateless_entering_queued_applies(self):
        from scalp import order_state as os_
        async def _run():
            client, db = _get_motor_db()
            try:
                oid = ObjectId()
                await db.trades.insert_one({
                    "_id": oid, "marker": TEST_MARKER,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                })
                out = await os_.apply(db, str(oid), os_.QUEUED, f"q:{oid}")
                doc = await db.trades.find_one({"_id": oid})
                return out, doc
            finally:
                await db.trades.delete_one({"_id": oid})
                client.close()
        out, doc = self._run_async(_run())
        assert out == "applied", f"expected 'applied', got {out}"
        assert doc.get("lifecycle_version") == 1, doc.get("lifecycle_version")
        assert doc.get("lifecycle_state") == "QUEUED"
        assert doc.get("lifecycle_quarantined") is not True

    def test_unverified_stateless_post_epoch_deep_state_quarantined(self):
        from scalp import order_state as os_
        async def _run():
            client, db = _get_motor_db()
            try:
                oid = ObjectId()
                await db.trades.insert_one({
                    "_id": oid, "marker": TEST_MARKER,
                    # AFTER LEGACY_EPOCH (2026-07-22) → unverified stateless
                    "created_at": "2026-07-23T00:00:00+00:00",
                })
                out = await os_.apply(db, str(oid), os_.CLOSED, f"c:{oid}")
                doc = await db.trades.find_one({"_id": oid})
                return out, doc
            finally:
                await db.trades.delete_one({"_id": oid})
                client.close()
        out, doc = self._run_async(_run())
        assert out == "quarantined", f"expected 'quarantined', got {out}"
        assert doc.get("lifecycle_quarantined") is True
        assert "refused" in (doc.get("lifecycle_quarantine_reason") or "")
        assert doc.get("lifecycle_state") in (None,)  # never set

    def test_legacy_stateless_pre_epoch_applies(self):
        from scalp import order_state as os_
        async def _run():
            client, db = _get_motor_db()
            try:
                oid = ObjectId()
                await db.trades.insert_one({
                    "_id": oid, "marker": TEST_MARKER,
                    # BEFORE LEGACY_EPOCH → verified legacy stateless
                    "created_at": "2026-01-01T00:00:00+00:00",
                })
                out = await os_.apply(db, str(oid), os_.CLOSED, f"c:{oid}")
                doc = await db.trades.find_one({"_id": oid})
                return out, doc
            finally:
                await db.trades.delete_one({"_id": oid})
                client.close()
        out, doc = self._run_async(_run())
        assert out == "applied", f"expected 'applied' for legacy doc, got {out}"
        assert doc.get("lifecycle_state") == "CLOSED"
        assert doc.get("lifecycle_quarantined") is not True


# =============================================================== item 3
# Unified reservation release via transition()
class TestUnifiedReservationRelease:
    def _run_async(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_release_applies_and_second_call_is_noop(self):
        from scalp import risk_reservations as rr
        rid = f"iter144_{uuid.uuid4().hex[:12]}"
        trade_id = "RTEST1"

        async def _run():
            client, db = _get_motor_db()
            try:
                await db.risk_reservations.insert_one({
                    "reservation_id": rid, "marker": TEST_MARKER,
                    "trade_id": trade_id, "state": "SLOT_LINKED",
                    "active": True, "transition_keys": [],
                    "transitions": [{"state": "SLOT_LINKED",
                                     "at": datetime.now(timezone.utc)}],
                    "account_id": "acc_iter144",
                    "user_id": "u_iter144", "decision_id": f"d_{rid}",
                    "risk_usd": 10.0, "lot": 0.01, "uncertain": False,
                    "created_at": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc),
                })
                # First release
                await rr.release_for_trade(db, trade_id, "closed")
                doc1 = await db.risk_reservations.find_one(
                    {"reservation_id": rid})
                transitions_count_1 = len(doc1.get("transitions") or [])
                keys_1 = list(doc1.get("transition_keys") or [])

                # Second release — should be a no-op via idempotency guard
                await rr.release_for_trade(db, trade_id, "closed")
                doc2 = await db.risk_reservations.find_one(
                    {"reservation_id": rid})
                transitions_count_2 = len(doc2.get("transitions") or [])
                keys_2 = list(doc2.get("transition_keys") or [])
                return doc1, doc2, transitions_count_1, transitions_count_2, \
                    keys_1, keys_2
            finally:
                await db.risk_reservations.delete_one({"reservation_id": rid})
                client.close()

        doc1, doc2, tc1, tc2, k1, k2 = self._run_async(_run())
        assert doc1.get("state") == "RELEASED", doc1.get("state")
        assert doc1.get("active") is False, doc1.get("active")
        assert doc1.get("release_reason") == "closed", doc1.get("release_reason")
        # transition_keys must contain the RELEASED:closed key
        assert "RELEASED:closed" in k1, k1
        # Second call is a no-op
        assert tc2 == tc1, (
            f"transitions grew on replay: {tc1} -> {tc2}")
        assert k2 == k1, f"transition_keys mutated on replay: {k1} -> {k2}"
        assert doc2.get("state") == "RELEASED"
        assert doc2.get("active") is False
