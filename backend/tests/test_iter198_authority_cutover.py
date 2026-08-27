"""iter-198 — v56 hard cutover + P0 timeout classification.
1) Every engine.execute call is now: Strategy → ExecutionIntent →
   Execution Authority (validate → authorize) → engine stage; the intent
   is the INPUT to execution.
2) Executor failures classify as PRE_DISPATCH (rejected) vs
   POST_DISPATCH (UNKNOWN — never false certainty)."""
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


class StubEngine:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {"id": "trade-stub", "status": "pending"}

    async def execute_authorized(self, *, user_id, account, signal,
                                 max_concurrent=0, cfg_account_id=None,
                                 intent=None, authorization=None):
        self.calls.append(intent)
        return dict(self.result)


def _signal(**over):
    doc = {"symbol": "XAUUSD", "action": "BUY", "lot_size": 0.1,
           "entry_price": 2400.0, "stop_loss": 2390.0,
           "signal_id": f"sig-{uuid.uuid4().hex[:8]}", "origin": "test"}
    doc.update(over)
    return doc


ACCOUNT = {"_id": "acct-cutover", "account_number": "555", "server": "Demo"}


def _cleanup():
    _run(_db().execution_intents.delete_many(
        {"account_id": "acct-cutover"}))
    _run(_db().platform_state.delete_many({"_id": "trading_authority"}))


class TestHardCutover:
    def teardown_method(self):
        _cleanup()

    def test_intent_is_the_input_to_execution(self):
        """Pipeline order: intent minted FIRST, then validated, then
        authorized, and the ENGINE receives the intent as input."""
        from execution_authority import submit_intent
        engine = StubEngine()
        result = _run(submit_intent(user_id="u1", account=ACCOUNT,
                                    signal=_signal(), engine=engine))
        assert result["id"] == "trade-stub"
        assert len(engine.calls) == 1
        intent = engine.calls[0]
        assert intent is not None and intent["intent_id"].startswith("xin_")
        assert intent["payload"]["symbol"] == "XAUUSD"
        assert intent["payload"]["broker_account_number"] == "555"
        doc = _run(_db().execution_intents.find_one(
            {"intent_id": intent["intent_id"]}))
        history = [h["to"] for h in doc["history"]]
        assert history[:3] == ["created", "validated", "authorized"], \
            f"pipeline order broken: {history}"

    def test_duplicate_submit_never_reaches_engine(self):
        from execution_authority import submit_intent
        engine = StubEngine()
        sig = _signal()
        _run(submit_intent(user_id="u1", account=ACCOUNT, signal=sig,
                           engine=engine))
        second = _run(submit_intent(user_id="u1", account=ACCOUNT,
                                    signal=dict(sig), engine=engine))
        assert second["blocked"] == "duplicate_intent"
        assert len(engine.calls) == 1

    def test_validation_failure_rejects_and_releases_key(self):
        from execution_authority import submit_intent
        engine = StubEngine()
        result = _run(submit_intent(user_id="u1", account=ACCOUNT,
                                    signal=_signal(symbol="", lot_size=0),
                                    engine=engine))
        assert result["blocked"] == "intent_validation"
        assert len(engine.calls) == 0
        doc = _run(_db().execution_intents.find_one(
            {"intent_id": result["intent_id"]}))
        assert doc["status"] == "rejected"
        assert "dedupe_key" not in doc, \
            "pre-dispatch refusal must release the dedupe key"

    def test_authority_block_cancels_and_allows_later_retry(self):
        from execution_authority import submit_intent
        from trading_authority import set_platform_level
        db = _db()
        engine = StubEngine()
        sig = _signal()
        _run(set_platform_level(db, "CLOSE_ONLY", "cutover test", "test"))
        blocked = _run(submit_intent(user_id="u1", account=ACCOUNT,
                                     signal=sig, engine=engine))
        assert blocked["blocked"] == "trading_authority"
        assert blocked["authority_level"] == "CLOSE_ONLY"
        assert len(engine.calls) == 0
        doc = _run(db.execution_intents.find_one(
            {"intent_id": blocked["intent_id"]}))
        assert doc["status"] == "cancelled" and "dedupe_key" not in doc
        # restriction lifted → the SAME signal is a new logical action
        _run(set_platform_level(db, "FULL", "restored", "test"))
        result = _run(submit_intent(user_id="u1", account=ACCOUNT,
                                    signal=dict(sig), engine=engine))
        assert result.get("id") == "trade-stub"
        assert len(engine.calls) == 1

    def test_engine_block_cancels_intent(self):
        from execution_authority import submit_intent
        engine = StubEngine(result={"blocked": "safety_guardian"})
        result = _run(submit_intent(user_id="u1", account=ACCOUNT,
                                    signal=_signal(), engine=engine))
        assert result["blocked"] == "safety_guardian"
        doc = _run(_db().execution_intents.find_one(
            {"intent_id": result["intent_id"]}))
        assert doc["status"] == "cancelled" and "dedupe_key" not in doc

    def test_reduced_authority_halves_volume_before_engine(self):
        from execution_authority import submit_intent
        from trading_authority import set_platform_level
        db = _db()
        engine = StubEngine()
        sig = _signal(lot_size=0.2)
        _run(set_platform_level(db, "REDUCED", "cutover test", "test"))
        _run(submit_intent(user_id="u1", account=ACCOUNT, signal=sig,
                           engine=engine))
        assert sig["lot_size"] == 0.1
        assert sig["_authority_reduced"] is True

    def test_mt5_engine_execute_routes_through_authority(self):
        """The real engine's execute() is a shim into the authority —
        source-level proof of the cutover."""
        import inspect
        from execution import MT5BridgeEngine
        src = inspect.getsource(MT5BridgeEngine.execute)
        assert "submit_intent" in src
        assert hasattr(MT5BridgeEngine, "execute_authorized")


class TestTimeoutClassification:
    def test_classifier(self):
        from execution_intents import PreDispatchError, request_never_left
        assert request_never_left(PreDispatchError("x")) is True
        assert request_never_left(ValueError("broker error 400")) is True
        assert request_never_left(PermissionError("bad creds")) is True
        assert request_never_left(ConnectionRefusedError()) is True
        ConnectError = type("ConnectError", (Exception,), {})
        ConnectTimeout = type("ConnectTimeout", (Exception,), {})
        assert request_never_left(ConnectError()) is True
        assert request_never_left(ConnectTimeout()) is True
        # request may be in flight → UNKNOWN
        assert request_never_left(TimeoutError()) is False
        ReadTimeout = type("ReadTimeout", (Exception,), {})
        assert request_never_left(ReadTimeout()) is False
        # unknown failure mode → never infer safety
        assert request_never_left(RuntimeError("connection dropped")) \
            is False

    def test_unknown_from_timeout_reconciles_by_broker_truth(self):
        """POST_DISPATCH_TIMEOUT → UNKNOWN; when broker truth later shows
        the trade open, the intent RECONCILES — never a second send."""
        from execution_intents import (dedupe_key_for,
                                       reconcile_unknown_intents, run_once)
        db = _db()
        key = dedupe_key_for("test", "open_trade", uuid.uuid4().hex)
        trade_r = _run(db.trades.insert_one(
            {"status": "open", "symbol": "XAUUSD", "mt5_ticket": 515151,
             "user_id": "cutover-test"}))

        async def ex(intent):
            # simulate: order reached broker, response lost
            await db.execution_intents.update_one(
                {"intent_id": intent["intent_id"]},
                {"$set": {"result": {"trade_id": str(trade_r.inserted_id)}}})
            raise TimeoutError("response lost")
        try:
            with pytest.raises(TimeoutError):
                _run(run_once(db, source="test", kind="open_trade",
                              dedupe_key=key, executor=ex))
            doc = _run(db.execution_intents.find_one({"dedupe_key": key}))
            assert doc["status"] == "unknown"
            out = _run(reconcile_unknown_intents(db))
            assert out["reconciled"] >= 1
            doc = _run(db.execution_intents.find_one({"dedupe_key": key}))
            assert doc["status"] == "reconciled"
            assert "515151" in doc["history"][-1]["detail"]
        finally:
            _run(db.execution_intents.delete_many({"dedupe_key": key}))
            _run(db.trades.delete_many({"user_id": "cutover-test"}))


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.integration
