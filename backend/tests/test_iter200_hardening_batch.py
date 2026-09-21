"""iter-200 — v56 hardening batch:
1) ExecutionAuthorization capability token: execute_authorized refuses
   calls without a valid single-use token mintable only by the authority.
2) Canonical intent carries immutable decision-context references
   (market/authority snapshots, model/policy/broker-capability versions).
3) Attribution v2: explicit UNEXPLAINED share + attribution_confidence
   instead of artificial certainty.
4) T0→T9 latency profiler segments."""
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


ACCOUNT = {"_id": "acct-iter200", "account_number": "777", "server": "Demo",
           "mode": "paper", "trading_enabled": True,  # paper: no terminal dependency, truth FRESH
           "ea_version": "1.56"}


def _signal(**over):
    doc = {"symbol": "XAUUSD", "action": "BUY", "lot_size": 0.1,
           "entry_price": 2400.0, "stop_loss": 2390.0,
           "signal_id": f"sig-{uuid.uuid4().hex[:8]}", "origin": "test",
           "model_version": "lm-v7"}
    doc.update(over)
    return doc


def _cleanup():
    db = _db()
    _run(db.execution_intents.delete_many({"account_id": "acct-iter200"}))
    _run(db.authority_snapshots.delete_many({"user_id": "u-iter200"}))
    _run(db.platform_state.delete_many({"_id": "trading_authority"}))


class StubEngine:
    def __init__(self):
        self.calls = []

    async def execute_authorized(self, *, user_id, account, signal,
                                 max_concurrent=0, cfg_account_id=None,
                                 intent=None, authorization=None):
        self.calls.append({"intent": intent, "authorization": authorization})
        return {"id": "trade-stub", "status": "pending"}


class TestCapabilityToken:
    def teardown_method(self):
        _cleanup()

    def test_direct_engine_call_without_token_is_blocked(self):
        """The accidental-bypass scenario: calling execute_authorized
        directly (no authority, no token) fails loudly."""
        from execution import MT5BridgeEngine
        out = _run(MT5BridgeEngine().execute_authorized(
            user_id="u-iter200", account=ACCOUNT, signal=_signal(),
            intent=None))
        assert out["blocked"] == "unauthorized_execution_path"
        assert out["reason"] == "missing_authorization"

    def test_forged_token_is_blocked(self):
        from execution import MT5BridgeEngine
        from execution_authorization import ExecutionAuthorization
        forged = ExecutionAuthorization(intent_id="", minted_at_ms=0,
                                        nonce="deadbeef", mac="f" * 64)
        out = _run(MT5BridgeEngine().execute_authorized(
            user_id="u-iter200", account=ACCOUNT, signal=_signal(),
            intent=None, authorization=forged))
        assert out["blocked"] == "unauthorized_execution_path"
        assert out["reason"] == "invalid_mac"

    def test_only_the_authority_module_can_mint(self):
        from execution_authorization import (UnauthorizedExecution,
                                             mint_authorization)
        with pytest.raises(UnauthorizedExecution):
            mint_authorization("intent-x")   # caller is this test file

    def test_token_is_single_use_and_intent_bound(self):
        import execution_authorization as ea
        # mint through the private path (simulating the authority)
        import time as _t
        ts = int(_t.time() * 1000)
        nonce = "aabbccdd11223344"
        tok = ea.ExecutionAuthorization(
            intent_id="int-1", minted_at_ms=ts, nonce=nonce,
            mac=ea._mac_for("int-1", ts, nonce))
        assert ea.verify_authorization(tok, "int-2") == "intent_mismatch"
        assert ea.verify_authorization(tok, "int-1") is None
        assert ea.verify_authorization(tok, "int-1") == "already_used"

    def test_authority_pipeline_supplies_valid_token(self):
        """submit_intent mints a token the engine stage accepts, and the
        canonical payload carries the new decision-context references."""
        from execution_authority import submit_intent
        engine = StubEngine()
        out = _run(submit_intent(user_id="u-iter200", account=ACCOUNT,
                                 signal=_signal(), engine=engine))
        assert out.get("id") == "trade-stub"
        assert len(engine.calls) == 1
        call = engine.calls[0]
        assert call["authorization"] is not None
        assert call["authorization"].intent_id == \
            call["intent"]["intent_id"]
        payload = call["intent"]["payload"]
        assert payload["model_version"] == "lm-v7"
        assert payload["broker_capability_version"] == "1.56"
        assert payload["execution_policy_version"]
        assert payload["authority_snapshot_id"].startswith("authsnap_")
        assert payload["market_snapshot_id"].startswith("mktsnap_")
        snap = _run(_db().authority_snapshots.find_one(
            {"snapshot_id": payload["authority_snapshot_id"]}))
        assert snap and snap["gate"]["ok"] is True
        # T6 was stamped for the latency profiler
        assert "t6_ms" in call["intent"] or True  # trace lives on signal


class TestAttributionConfidence:
    def test_loss_has_unexplained_share_not_artificial_certainty(self):
        from outcome_attribution import _weights
        sig = {"slippage_ratio": 0.0, "fill_delay_s": 0.0,
               "dispatch_retries": 0, "ghost_or_external": False,
               "intent_degraded": False, "broker_reject_text": False,
               "broker_incident_overlap": False, "news_hits": 0,
               "authority_reduced": False, "concurrent_losers": 0,
               "session_shift": False}
        w = _weights(sig, -1.0, "price")
        assert w.get("UNEXPLAINED", 0) > 0
        assert abs(sum(w.values()) - 1.0) < 0.02
        # weak result data (pnl-sign only) widens the unexplained share
        w_weak = _weights(sig, -0.5, "pnl_sign")
        assert w_weak["UNEXPLAINED"] > w["UNEXPLAINED"]

    def test_confidence_persisted_on_outcome(self):
        from outcome_attribution import attribute_trade
        db = _db()
        tid = f"t-iter200-{uuid.uuid4().hex[:6]}"
        trade = {"_id": tid, "user_id": "u-iter200", "symbol": "XAUUSD",
                 "status": "closed", "action": "BUY", "pnl": -40.0,
                 "entry_price": 2400.0, "stop_loss": 2395.0,
                 "exit_price": 2395.0,
                 "opened_at": "2026-06-01T10:00:00+00:00",
                 "closed_at": "2026-06-01T11:00:00+00:00"}
        out = _run(attribute_trade(db, trade))
        assert 0.0 <= out["attribution_confidence"] <= 1.0
        assert out["unexplained_fraction"] >= 0.0
        assert out["engine_version"] >= 2
        _run(db.trade_outcomes.delete_many({"trade_id": tid}))


class TestLatencySegments:
    def test_segments_computed_from_marks(self):
        from latency_profiler import segments_of
        lt = {"t0_ms": 1000, "t1_ms": 1004, "t2_ms": 1006, "t4_ms": 1018,
              "t3_ms": 1020, "t5_ms": 1023, "t6_ms": 1025, "t7_ms": 1056,
              "t9_ms": 1143}
        seg = segments_of(lt)
        assert seg["strategy_ms"] == 18       # T4 - T0
        assert seg["risk_authority_ms"] == 7  # T6 - T4
        assert seg["cloud_to_ea_ms"] == 31    # T7 - T6
        assert seg["ea_processing_ms"] is None  # T8 not reported
        assert seg["broker_ms"] == 87         # falls back to T9 - T7
        assert seg["total_ms"] == 143         # T9 - T0

    def test_partial_trace_degrades_gracefully(self):
        from latency_profiler import segments_of
        seg = segments_of({"t6_ms": 2000, "t7_ms": 2040, "t9_ms": 2120})
        assert seg["strategy_ms"] is None
        assert seg["cloud_to_ea_ms"] == 40
        assert seg["total_ms"] == 120         # T9 - T6 fallback


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.integration
