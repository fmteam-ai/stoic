"""iter-124 · Independent P0 verification for v56 timeout classification &
hard-cutover per QA review request.

Only sanity-covers what the review asks that isn't already in
test_iter198_authority_cutover.py / test_iter195_execution_invariants.py:
  1. run_once with TimeoutError → status 'unknown', executor never re-runs
     on retry (duplicate + in_flight), then reconcile → 'reconciled'.
  2. run_once with a trade doc that is 'failed' → reconcile classifies as
     'failed_confirmed'.
  3. Pre-dispatch exception matrix: ValueError / PermissionError /
     ConnectionRefusedError / PreDispatchError → 'rejected'.
"""
import os
import sys
import uuid

import pytest

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)


def _run(coro):
    from conftest import run_async
    return run_async(coro)


def _db():
    from database import get_db
    return get_db()


def _cleanup(user_marker: str, dedupe_keys: list[str]):
    if dedupe_keys:
        _run(_db().execution_intents.delete_many(
            {"dedupe_key": {"$in": dedupe_keys}}))
    _run(_db().trades.delete_many({"user_id": user_marker}))


class TestTimeoutUnknownAndRetry:
    def test_timeout_yields_unknown_and_retry_returns_in_flight(self):
        from execution_intents import (dedupe_key_for, run_once,
                                       reconcile_unknown_intents)
        db = _db()
        marker = f"iter124-{uuid.uuid4().hex[:6]}"
        key = dedupe_key_for("test", "open_trade", uuid.uuid4().hex)
        # broker truth doc — simulates "response lost but order filled"
        trade_r = _run(db.trades.insert_one(
            {"status": "open", "symbol": "XAUUSD", "mt5_ticket": 909090,
             "user_id": marker}))

        calls = {"n": 0}

        async def timing_out_executor(intent):
            calls["n"] += 1
            # first call stashes result.trade_id then raises TimeoutError
            await db.execution_intents.update_one(
                {"intent_id": intent["intent_id"]},
                {"$set": {"result": {"trade_id": str(trade_r.inserted_id)}}})
            raise TimeoutError("simulated broker read timeout")

        try:
            with pytest.raises(TimeoutError):
                _run(run_once(db, source="test", kind="open_trade",
                              dedupe_key=key,
                              executor=timing_out_executor))
            doc = _run(db.execution_intents.find_one({"dedupe_key": key}))
            assert doc["status"] == "unknown", \
                f"expected unknown, got {doc['status']}"
            assert calls["n"] == 1

            # retry MUST NOT re-execute — dedupe-guard returns in_flight
            retry = _run(run_once(db, source="test", kind="open_trade",
                                  dedupe_key=key,
                                  executor=timing_out_executor))
            assert calls["n"] == 1, "executor MUST NOT re-run for unknown"
            assert retry["duplicate"] is True
            assert retry["in_flight"] is True
            assert retry["status"] == "unknown"

            # broker truth reconciliation → 'reconciled'
            out = _run(reconcile_unknown_intents(db))
            assert out["reconciled"] >= 1
            doc = _run(db.execution_intents.find_one({"dedupe_key": key}))
            assert doc["status"] == "reconciled"
            assert "909090" in doc["history"][-1]["detail"]
        finally:
            _cleanup(marker, [key])

    def test_unknown_with_failed_trade_reconciles_failed_confirmed(self):
        from execution_intents import (dedupe_key_for, run_once,
                                       reconcile_unknown_intents)
        db = _db()
        marker = f"iter124f-{uuid.uuid4().hex[:6]}"
        key = dedupe_key_for("test", "open_trade", uuid.uuid4().hex)
        trade_r = _run(db.trades.insert_one(
            {"status": "failed", "symbol": "XAUUSD",
             "user_id": marker}))

        async def ex(intent):
            await db.execution_intents.update_one(
                {"intent_id": intent["intent_id"]},
                {"$set": {"result": {"trade_id": str(trade_r.inserted_id)}}})
            raise TimeoutError("simulated timeout")

        try:
            with pytest.raises(TimeoutError):
                _run(run_once(db, source="test", kind="open_trade",
                              dedupe_key=key, executor=ex))
            out = _run(reconcile_unknown_intents(db))
            assert out["failed_confirmed"] >= 1
            doc = _run(db.execution_intents.find_one({"dedupe_key": key}))
            assert doc["status"] == "failed_confirmed"
        finally:
            _cleanup(marker, [key])


class TestPreDispatchRejects:
    @pytest.mark.parametrize("exc_factory,label", [
        (lambda: ValueError("broker error 400 invalid symbol"), "value"),
        (lambda: PermissionError("bad creds"), "permission"),
        (lambda: ConnectionRefusedError("connect refused"), "conn_refused"),
    ])
    def test_pre_dispatch_reject(self, exc_factory, label):
        from execution_intents import dedupe_key_for, run_once
        db = _db()
        key = dedupe_key_for("test", "open_trade", label,
                             uuid.uuid4().hex)

        async def ex(_intent):
            raise exc_factory()

        try:
            with pytest.raises(BaseException):
                _run(run_once(db, source="test", kind="open_trade",
                              dedupe_key=key, executor=ex))
            doc = _run(db.execution_intents.find_one({"dedupe_key": key}))
            assert doc["status"] == "rejected", \
                f"{label}: got {doc['status']}"
        finally:
            _run(db.execution_intents.delete_many({"dedupe_key": key}))

    def test_pre_dispatch_error_reject(self):
        from execution_intents import (PreDispatchError, dedupe_key_for,
                                       run_once)
        db = _db()
        key = dedupe_key_for("test", "open_trade", "pde",
                             uuid.uuid4().hex)

        async def ex(_intent):
            raise PreDispatchError("never left")

        try:
            with pytest.raises(PreDispatchError):
                _run(run_once(db, source="test", kind="open_trade",
                              dedupe_key=key, executor=ex))
            doc = _run(db.execution_intents.find_one({"dedupe_key": key}))
            assert doc["status"] == "rejected"
        finally:
            _run(db.execution_intents.delete_many({"dedupe_key": key}))


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.integration
