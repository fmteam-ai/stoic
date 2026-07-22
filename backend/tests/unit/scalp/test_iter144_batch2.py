"""iter-144 · Architectural hardening Batch 2 (audit r3):
1. legacy lifecycle quarantine  2. unified reservation release
3. EA command sequence fencing  4. startup failure → readiness abort."""
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import asyncio
import inspect

import pytest
from unittest.mock import AsyncMock, MagicMock

from scalp import order_state as os_
from scalp import risk_reservations as rr
import command_fence as cf


class _Cursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._docs:
            raise StopAsyncIteration
        return self._docs.pop(0)


def _bridge_src():
    return open(_os.path.join(_BACKEND_DIR, "routes/bridge_routes.py")).read()


def _engine_src():
    import scalp.engine as e
    return inspect.getsource(e)


def _server_src():
    return open(_os.path.join(_BACKEND_DIR, "server.py")).read()


# ---------------------------------------------------------------- item 1
class TestLegacyQuarantine:
    def _db(self, modified=0, doc=None):
        db = MagicMock()
        db.trades.update_one = AsyncMock(
            return_value=MagicMock(modified_count=modified))
        db.trades.find_one = AsyncMock(return_value=doc)
        return db

    def test_legacy_epoch_defined_iso(self):
        assert isinstance(os_.LEGACY_EPOCH, str)
        assert os_.LEGACY_EPOCH.startswith("2026-")
        assert "+00:00" in os_.LEGACY_EPOCH

    def test_entry_state_allows_stateless(self):
        db = self._db(modified=1)
        out = asyncio.run(os_.apply(db, "t1", os_.QUEUED, "q:t1"))
        assert out == "applied"

    def test_non_entry_guard_requires_legacy_verification(self):
        db = self._db(modified=1)
        asyncio.run(os_.apply(db, "t1", os_.CLOSED, "c:t1"))
        q = db.trades.update_one.await_args_list[0].args[0]
        stateless_branch = q["$or"][0]
        # stateless entry to a deep state must be $and-ed with legacy proof
        assert "$and" in stateless_branch
        legacy = stateless_branch["$and"][1]["$or"]
        keys = {list(c.keys())[0] for c in legacy}
        assert {"lifecycle_version", "legacy_trade",
                "created_at", "opened_at"} <= keys

    def test_unverified_stateless_is_quarantined(self):
        doc = {"_id": "t1", "lifecycle_state": None, "lifecycle_keys": [],
               "created_at": "2026-07-23T00:00:00+00:00"}
        db = self._db(modified=0, doc=doc)
        out = asyncio.run(os_.apply(db, "t1", os_.CLOSED, "c:t1"))
        assert out == "quarantined"
        qset = db.trades.update_one.await_args_list[1].args[1]["$set"]
        assert qset["lifecycle_quarantined"] is True
        assert "refused CLOSED" in qset["lifecycle_quarantine_reason"]

    def test_duplicate_key_still_wins_over_quarantine(self):
        doc = {"_id": "t1", "lifecycle_state": None,
               "lifecycle_keys": ["c:t1"]}
        db = self._db(modified=0, doc=doc)
        out = asyncio.run(os_.apply(db, "t1", os_.CLOSED, "c:t1"))
        assert out == "duplicate"

    def test_invalid_transition_unchanged(self):
        doc = {"_id": "t1", "lifecycle_state": os_.CLOSED,
               "lifecycle_keys": []}
        db = self._db(modified=0, doc=doc)
        out = asyncio.run(os_.apply(db, "t1", os_.EA_CLAIMED, "x:t1"))
        assert out == "invalid"

    def test_applied_stamps_lifecycle_version(self):
        db = self._db(modified=1)
        asyncio.run(os_.apply(db, "t1", os_.QUEUED, "q:t1"))
        s = db.trades.update_one.await_args_list[0].args[1]["$set"]
        assert s["lifecycle_version"] == 1


# ---------------------------------------------------------------- item 2
class TestUnifiedReservationRelease:
    def test_no_raw_update_many(self):
        src = inspect.getsource(rr.release_for_trade)
        assert "update_many" not in src
        assert "transition" in src

    def test_release_routes_through_guarded_transition(self):
        db = MagicMock()
        db.risk_reservations.find = MagicMock(return_value=_Cursor(
            [{"reservation_id": "r1"}, {"reservation_id": "r2"}]))
        db.risk_reservations.update_one = AsyncMock(
            return_value=MagicMock(modified_count=1))
        asyncio.run(rr.release_for_trade(db, "t1", "closed"))
        calls = db.risk_reservations.update_one.await_args_list
        assert len(calls) == 2
        for c, rid in zip(calls, ("r1", "r2")):
            q, upd = c.args
            assert q["reservation_id"] == rid
            assert set(q["state"]["$in"]) == set(rr.ACTIVE_STATES)
            assert "$ne" in q["transition_keys"]      # idempotency guard
            assert upd["$set"]["state"] == "RELEASED"
            assert upd["$set"]["active"] is False
            assert upd["$set"]["release_reason"] == "closed"

    def test_release_noop_without_active_reservations(self):
        db = MagicMock()
        db.risk_reservations.find = MagicMock(return_value=_Cursor([]))
        db.risk_reservations.update_one = AsyncMock()
        asyncio.run(rr.release_for_trade(db, "t1", "closed"))
        db.risk_reservations.update_one.assert_not_awaited()


# ---------------------------------------------------------------- item 3
class TestCommandFence:
    def test_stamp_is_single_atomic_pipeline(self):
        db = MagicMock()
        db.trades.find_one_and_update = AsyncMock(return_value={
            "pending_modification": {"intent_id": "abc", "seq": 3}})
        out = asyncio.run(cf.stamp_pending_modification(
            db, {"_id": "t1", "pending_modification": None},
            {"type": "MODIFY_SL", "new_sl": 1.23},
            extra_set={"protection.state": "REARM_REQUESTED"}))
        assert out == {"intent_id": "abc", "seq": 3}
        args = db.trades.find_one_and_update.await_args
        filt, pipeline = args.args[0], args.args[1]
        assert filt["pending_modification"] is None
        assert isinstance(pipeline, list) and len(pipeline) == 2
        seq_stage = pipeline[0]["$set"]["command_seq"]
        assert seq_stage == {"$add": [{"$ifNull": ["$command_seq", 0]}, 1]}
        mod = pipeline[1]["$set"]["pending_modification"]
        assert mod["seq"] == "$command_seq"          # same-pipeline reference
        assert mod["type"] == {"$literal": "MODIFY_SL"}
        assert "intent_id" in mod and "requested_at" in mod
        extra = pipeline[1]["$set"]["protection.state"]
        assert extra == {"$literal": "REARM_REQUESTED"}

    def test_stamp_returns_none_when_filter_misses(self):
        db = MagicMock()
        db.trades.find_one_and_update = AsyncMock(return_value=None)
        out = asyncio.run(cf.stamp_pending_modification(
            db, {"_id": "t1", "pending_modification": None},
            {"type": "FULL_CLOSE"}))
        assert out is None

    def test_stamp_generates_unique_intents(self):
        db = MagicMock()
        db.trades.find_one_and_update = AsyncMock(return_value=None)
        asyncio.run(cf.stamp_pending_modification(db, {}, {"type": "X"}))
        asyncio.run(cf.stamp_pending_modification(db, {}, {"type": "X"}))
        i1 = db.trades.find_one_and_update.await_args_list[0] \
            .args[1][1]["$set"]["pending_modification"]["intent_id"]
        i2 = db.trades.find_one_and_update.await_args_list[1] \
            .args[1][1]["$set"]["pending_modification"]["intent_id"]
        assert i1["$literal"] != i2["$literal"]

    def test_is_stale_ack_matrix(self):
        cur = {"pending_modification": {"intent_id": "new"}}
        assert cf.is_stale_ack(cur, None) is False           # pre-v1.49 EA
        assert cf.is_stale_ack(cur, "new") is False          # current cmd
        assert cf.is_stale_ack(cur, "old") is True           # superseded
        assert cf.is_stale_ack({}, "old") is False           # nothing pending
        done = {"executed_intents": ["old"], "pending_modification": None}
        assert cf.is_stale_ack(done, "old") is True          # replay
        assert cf.is_stale_ack(done, "fresh") is False

    def test_ack_endpoint_fenced_and_records_intents(self):
        src = _bridge_src()
        assert "is_stale_ack(trade, payload.intent_id)" in src
        assert "stale_intent_ignored" in src
        assert '"executed_intents": {' in src
        assert '"$slice": -50' in src
        assert "intent_id: str | None = None" in src   # BridgeModificationAck

    def test_engine_commands_are_stamped(self):
        src = _engine_src()
        assert src.count("stamp_pending_modification") >= 6  # 3 imports + 3 calls
        # _request_close no longer writes pending_modification raw
        rc = inspect.getsource(
            __import__("scalp.engine", fromlist=["ScalpRunner"])
            .ScalpRunner._request_close)
        assert "stamp_pending_modification" in rc
        assert '"$set": {"pending_modification"' not in rc

    def test_heartbeat_rearm_and_escalation_stamped(self):
        src = _bridge_src()
        assert '"reason": "protection_rearm"' in src
        assert '"reason": "unprotected_position"' in src
        # both go through the fence, not a raw $set
        assert src.count("stamp_pending_modification") >= 4  # 2 imports + 2 calls


# ---------------------------------------------------------------- item 4
class TestStartupFailureReadiness:
    def test_startup_error_flag_wired(self):
        src = _server_src()
        assert "_startup_error: str | None = None" in src
        assert 'checks["startup"]' in src
        assert "_startup_error = f\"{type(e).__name__}\"" in src

    def test_production_startup_reraises(self):
        src = _server_src()
        seg = src[src.index("Startup error"):]
        assert '"APP_ENV", "").lower() == "production"' in seg[:600]
        assert "raise" in seg[:600]

    def test_readiness_fails_on_startup_error(self):
        src = _server_src()
        i = src.index('checks["startup"]')
        seg = src[i:i + 200]
        assert "ok = False" in seg
