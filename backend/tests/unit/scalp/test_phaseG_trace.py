"""Phase G — trace observability: one trace id (the decision_id) from tick
to analytics, with stages, merged timeline and a deterministic narrative.
Unit portion is MongoDB-free."""
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import pytest

import trade_trace as tt

pytestmark = pytest.mark.unit


DECISION = {
    "decision_id": "d1", "user_id": "u1", "symbol": "EURUSD",
    "direction": "BUY", "ts_ms": 1_000_000, "signal_ts_ms": 999_900,
    "model_key": "ic|demo|EURUSD", "model_source": "model",
    "setup": {"kind": "impulse_pullback"}, "setup_preset": "pullback_default",
    "features": {"spread_pips": 0.6, "vol_short": 1.2, "mom_pips": 2.4},
    "forecast": {"p_target_before_stop": 0.61},
    "net_edge_pips": 0.8, "ev": {"ev_usd": 0.42},
    "decision_quality": {"score": 71.2, "verdict": "STRONG",
                         "pillars": {"ev": 0.5}, "weakest": ["ev", "risk"]},
    "verdict": "live_traded", "risk": {"lot": 0.02, "vol_size_mult": 0.8},
}
TRADE = {
    "_id": "t1", "scalp_decision_id": "d1", "status": "closed",
    "entry_price": 1.0851, "stop_loss": 1.0847, "take_profit": 1.0856,
    "close_reason": "scalp_adaptive_ev_negative", "profit": 1.2,
    "lifecycle_state": "FINANCIALLY_RECONCILED",
    "lifecycle": [{"state": "QUEUED", "at": "2026-06-01T10:00:00+00:00"},
                  {"state": "OPEN", "at": "2026-06-01T10:00:01+00:00"}],
    "adaptive_actions": [{"ts_ms": 1_030_000, "action": "TIGHTEN_STOP",
                          "reason": "adaptive_vol_trail"}],
}


class TestTimestampNormalisation:
    def test_ms_handles_all_shapes(self):
        from datetime import datetime, timezone
        assert tt._ms(1500) == 1500
        assert tt._ms("1970-01-01T00:00:01+00:00") == 1000
        assert tt._ms(datetime(1970, 1, 1, 0, 0, 2,
                               tzinfo=timezone.utc)) == 2000
        assert tt._ms(None) is None
        assert tt._ms("garbage") is None


class TestStages:
    def test_all_eight_stages_present(self):
        st = tt.build_stages(DECISION, TRADE, [], [], None)
        assert set(tt.STAGE_ORDER) <= set(st)
        assert st["tick"]["spread_pips"] == 0.6
        assert st["model"]["p_target_before_stop"] == 0.61
        assert st["model"]["decision_quality"]["verdict"] == "STRONG"
        assert st["risk"]["lot"] == 0.02
        assert st["oms"]["lifecycle_state"] == "FINANCIALLY_RECONCILED"
        assert st["broker"]["entry_price"] == 1.0851


class TestTimeline:
    def test_merged_and_ordered(self):
        events = [{"ts_ms": 1_010_000, "event_type": "BrokerSubmitted",
                   "payload": {"lot": 0.02}}]
        resv = [{"transitions": [
            {"state": "RISK_RESERVED", "at": "1970-01-01T00:16:45+00:00"}]}]
        fins = [{"deal_id": "77", "event_type": "CLOSE",
                 "net_pnl_usd": 1.2,
                 "created_at": "1970-01-01T00:17:30+00:00"}]
        tl = tt.build_timeline(DECISION, TRADE, events, resv, fins)
        ts = [x["ts_ms"] for x in tl]
        assert ts == sorted(ts)
        whats = " | ".join(x["what"] for x in tl)
        assert "decision live_traded" in whats
        assert "reservation RISK_RESERVED" in whats
        assert "order QUEUED" in whats
        assert "BrokerSubmitted" in whats
        assert "adaptive TIGHTEN_STOP" in whats
        assert "deal 77" in whats


class TestNarrative:
    def test_full_story_for_a_closed_trade(self):
        n = " ".join(tt.build_narrative(DECISION, TRADE))
        assert "EURUSD BUY" in n
        assert "61%" in n
        assert "0.8 pips" in n
        assert "71.2/100" in n
        assert "0.02 lots" in n
        assert "1.0851" in n
        assert "$1.2" in n

    def test_rejected_decision_explains_and_stops(self):
        d = {**DECISION, "verdict": "rejected",
             "reject_stage": "combined_decision_quality"}
        n = tt.build_narrative(d, None)
        assert any("REJECTED at stage 'combined_decision_quality'" in s
                   for s in n)
        assert not any("lots" in s for s in n)


class TestWiring:
    def test_route_registered(self):
        src = open(_os.path.join(_BACKEND_DIR, "server.py")).read()
        assert "trace_router" in src
        rsrc = open(_os.path.join(_BACKEND_DIR, "routes/trace_routes.py")).read()
        assert "assemble_trace" in rsrc
        assert "404" in rsrc                       # ownership → not found

    def test_stop_modify_events_in_registry(self):
        # Phase G audit fix: round-18 stop-modify events were missing from
        # EVENT_TYPES and silently failed validation
        from trade_events import EVENT_TYPES
        assert "StopModifyConfirmed" in EVENT_TYPES
        assert "StopModifyRejected" in EVENT_TYPES
