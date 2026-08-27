"""iter-195 — v56 execution-invariant suite (§19/§22). Every scenario
proves the formal invariant: ONE execution_intent_id → AT MOST ONE
logical broker action, across retries, crashes, reconnects, timeouts
and lost acknowledgements. UNKNOWN never resends — broker truth decides."""
import os
import sys
import uuid

import pytest

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)
from dotenv import load_dotenv
load_dotenv(os.path.join(_BACKEND_DIR, ".env"))


def _run(coro):
    from conftest import run_async
    return run_async(coro)


def _db():
    from database import get_db
    return get_db()


def _key():
    from execution_intents import dedupe_key_for
    return dedupe_key_for("invariant", "open_trade", uuid.uuid4().hex)


def _cleanup(keys=None, intent_ids=None):
    db = _db()
    if keys:
        _run(db.execution_intents.delete_many(
            {"dedupe_key": {"$in": list(keys)}}))
    if intent_ids:
        _run(db.execution_intents.delete_many(
            {"intent_id": {"$in": list(intent_ids)}}))


class TestExecutionInvariants:

    def test_01_duplicate_intent(self):
        """Same dedupe key twice → executor runs exactly once."""
        from execution_intents import run_once
        key, calls = _key(), []

        async def ex(i):
            calls.append(1)
            return {"ticket": 1}
        try:
            _run(run_once(_db(), source="t", kind="open_trade",
                          dedupe_key=key, executor=ex))
            _run(run_once(_db(), source="t", kind="open_trade",
                          dedupe_key=key, executor=ex))
            assert len(calls) == 1
        finally:
            _cleanup(keys=[key])

    def test_02_lost_ack_retry_returns_original(self):
        """Caller loses the API response and retries → stored result
        returned, zero re-execution."""
        from execution_intents import run_once
        key, calls = _key(), []

        async def ex(i):
            calls.append(1)
            return {"ticket": 77}
        try:
            _run(run_once(_db(), source="t", kind="open_trade",
                          dedupe_key=key, executor=ex))
            retry = _run(run_once(_db(), source="t", kind="open_trade",
                                  dedupe_key=key, executor=ex))
            assert retry["duplicate"] is True
            assert retry["result"] == {"ticket": 77}
            assert len(calls) == 1
        finally:
            _cleanup(keys=[key])

    def test_03_worker_crash_mid_flight(self):
        """Worker claims the intent then dies. A retry must NOT re-execute;
        the intent resolves via broker-truth reconciliation only."""
        from execution_intents import (create_intent, mark_unknown_stale,
                                       reconcile_unknown_intents, run_once)
        db, key, calls = _db(), _key(), []
        intent = _run(create_intent(db, source="t", kind="open_trade",
                                    dedupe_key=key))
        # simulate crash: claimed (submitted) but no result ever recorded
        _run(db.execution_intents.update_one(
            {"intent_id": intent["intent_id"]},
            {"$set": {"status": "submitted",
                      "created_at": "2020-01-01T00:00:00+00:00"}}))

        async def ex(i):
            calls.append(1)
            return {}
        try:
            retry = _run(run_once(db, source="t", kind="open_trade",
                                  dedupe_key=key, executor=ex))
            assert retry["duplicate"] is True and retry["in_flight"] is True
            assert len(calls) == 0, "retry re-executed a crashed intent!"
            _run(mark_unknown_stale(db))
            doc = _run(db.execution_intents.find_one({"dedupe_key": key}))
            assert doc["status"] == "unknown"
            _run(reconcile_unknown_intents(db))
            doc = _run(db.execution_intents.find_one({"dedupe_key": key}))
            assert doc["status"] == "unknown", \
                "no broker truth exists — must stay UNKNOWN, never resend"
            assert len(calls) == 0
        finally:
            _cleanup(keys=[key])

    def test_04_agent_reconnect_duplicate_dispatch(self):
        """Host agent reconnects and re-dispatches → the second acked
        transition is refused (state machine idempotency)."""
        from execution_intents import create_intent, transition
        db = _db()
        it = _run(create_intent(db, source="t", kind="open_trade"))
        try:
            _run(transition(db, it["intent_id"], "submitted"))
            first = _run(transition(db, it["intent_id"], "acked",
                                    detail="dispatch"))
            second = _run(transition(db, it["intent_id"], "acked",
                                     detail="re-dispatch"))
            assert first is not None and second is None
            doc = _run(db.execution_intents.find_one(
                {"intent_id": it["intent_id"]}))
            assert [h["to"] for h in doc["history"]].count("acked") == 1
        finally:
            _cleanup(intent_ids=[it["intent_id"]])

    def test_05_ea_reconnect_duplicate_report(self):
        """EA reconnects and reports the same fill twice → one terminal
        state, duplicate report refused."""
        from execution_intents import create_intent, transition
        db = _db()
        it = _run(create_intent(db, source="t", kind="open_trade"))
        try:
            _run(transition(db, it["intent_id"], "submitted"))
            assert _run(transition(db, it["intent_id"], "filled",
                                   result={"ticket": 9})) is not None
            assert _run(transition(db, it["intent_id"], "filled")) is None
            assert _run(transition(db, it["intent_id"], "rejected")) is None
            doc = _run(db.execution_intents.find_one(
                {"intent_id": it["intent_id"]}))
            assert doc["status"] == "filled"
            assert doc["result"] == {"ticket": 9}
        finally:
            _cleanup(intent_ids=[it["intent_id"]])

    def test_06_stale_fencing_epoch_converges(self):
        """A stale ex-owner replaying its order converges on the original
        intent (same identity → same dedupe key → no second action) and
        the canonical payload preserves the fencing epoch for the
        dispatch fence."""
        from execution_intents import (canonical_payload, create_intent,
                                       dedupe_key_for)
        db = _db()
        key = dedupe_key_for("scalp_fast", "open_trade", "acct1",
                             "XAUUSD", "BUY", "sig-epoch-1")
        payload = canonical_payload(
            account_id="acct1", broker_account_number="123",
            broker_server="Demo", strategy_id="scalp_fast",
            strategy_version="1", symbol="XAUUSD", side="BUY",
            requested_volume=0.1, stop_loss=None, take_profit=None,
            risk_snapshot_id="rs", signal_id="sig-epoch-1",
            fencing_epoch=1, nonce="n1")
        try:
            first = _run(create_intent(db, source="scalp_fast",
                                       kind="open_trade", dedupe_key=key,
                                       payload=payload))
            assert first["payload"]["fencing_epoch"] == 1
            replay = _run(create_intent(db, source="scalp_fast",
                                        kind="open_trade", dedupe_key=key,
                                        payload=payload))
            assert replay["duplicate"] is True
            assert replay["intent_id"] == first["intent_id"]
        finally:
            _cleanup(keys=[key])

    def test_07_expired_intent_never_reexecutes(self):
        from execution_intents import create_intent, expire_stale, run_once
        db, key, calls = _db(), _key(), []
        it = _run(create_intent(db, source="t", kind="open_trade",
                                dedupe_key=key))
        _run(db.execution_intents.update_one(
            {"intent_id": it["intent_id"]},
            {"$set": {"created_at": "2020-01-01T00:00:00+00:00"}}))

        async def ex(i):
            calls.append(1)
            return {}
        try:
            _run(expire_stale(db))
            doc = _run(db.execution_intents.find_one({"dedupe_key": key}))
            assert doc["status"] == "expired"
            retry = _run(run_once(db, source="t", kind="open_trade",
                                  dedupe_key=key, executor=ex))
            assert retry["duplicate"] is True
            assert len(calls) == 0
        finally:
            _cleanup(keys=[key])

    def test_08_broker_timeout_marks_rejected_no_retry_execution(self):
        from execution_intents import run_once
        db, key, calls = _db(), _key(), []

        async def ex(i):
            calls.append(1)
            raise TimeoutError("broker timed out")
        try:
            with pytest.raises(TimeoutError):
                _run(run_once(db, source="t", kind="open_trade",
                              dedupe_key=key, executor=ex))
            retry = _run(run_once(db, source="t", kind="open_trade",
                                  dedupe_key=key, executor=ex))
            assert retry["duplicate"] is True
            assert len(calls) == 1, "timeout retry re-executed!"
            doc = _run(db.execution_intents.find_one({"dedupe_key": key}))
            assert doc["status"] == "rejected"
        finally:
            _cleanup(keys=[key])

    def test_09_broker_executed_but_response_lost(self):
        """The most dangerous case: broker DID execute, response was lost.
        UNKNOWN → query stored broker truth → RECONCILED. No second BUY."""
        from execution_intents import (create_intent, mark_unknown_stale,
                                       reconcile_unknown_intents,
                                       transition)
        db = _db()
        trade_r = _run(db.trades.insert_one(
            {"status": "open", "symbol": "XAUUSD", "action": "BUY",
             "mt5_ticket": 424242, "user_id": "invariant-test",
             "opened_at": "2020-01-01T00:00:00+00:00"}))
        trade_id = str(trade_r.inserted_id)
        it = _run(create_intent(db, source="t", kind="open_trade"))
        _run(transition(db, it["intent_id"], "submitted",
                        result={"trade_id": trade_id}))
        _run(db.execution_intents.update_one(
            {"intent_id": it["intent_id"]},
            {"$set": {"created_at": "2020-01-01T00:00:00+00:00"}}))
        try:
            before = _run(db.trades.count_documents(
                {"user_id": "invariant-test"}))
            _run(mark_unknown_stale(db))
            out = _run(reconcile_unknown_intents(db))
            assert out["reconciled"] >= 1
            doc = _run(db.execution_intents.find_one(
                {"intent_id": it["intent_id"]}))
            assert doc["status"] == "reconciled"
            assert "424242" in doc["history"][-1]["detail"]
            after = _run(db.trades.count_documents(
                {"user_id": "invariant-test"}))
            assert after == before == 1, "reconciliation created a trade!"
        finally:
            _cleanup(intent_ids=[it["intent_id"]])
            from bson import ObjectId
            _run(db.trades.delete_many({"user_id": "invariant-test"}))
