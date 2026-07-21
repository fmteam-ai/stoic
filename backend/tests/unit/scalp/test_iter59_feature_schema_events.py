"""Iter-59 — feature versioning (review item 5) + trade_events stream (item 4)
+ EA-native constraint preflight plumbing (item 2). Pure unit tests."""
import pytest

from scalp.feature_schema import (FEATURE_SCHEMA_VERSION, FEATURE_SCHEMAS,
                                  current_keys, keys_for)
from scalp.features import FEATURE_KEYS
from scalp.model import vectorize
from trade_events import EVENT_TYPES, build
from models import BridgeSymbolSpec

pytestmark = pytest.mark.unit


class TestFeatureSchema:
    def test_current_version_is_1(self):
        assert FEATURE_SCHEMA_VERSION == 1
        assert 1 in FEATURE_SCHEMAS

    def test_feature_keys_match_schema_v1(self):
        assert FEATURE_KEYS == list(current_keys())
        assert keys_for(1) == FEATURE_SCHEMAS[1]

    def test_missing_version_defaults_to_v1(self):
        assert keys_for(None) == FEATURE_SCHEMAS[1]

    def test_unknown_version_returns_none(self):
        assert keys_for(99) is None
        assert keys_for("junk") is None

    def test_vectorize_respects_schema_version(self):
        feats = {k: 1.0 for k in FEATURE_KEYS}
        v = vectorize(feats)
        assert v == [1.0] * len(FEATURE_KEYS)
        assert vectorize(feats, 1) == v
        assert vectorize(feats, 99) is None       # unknown schema → no vector

    def test_vectorize_missing_feature_returns_none(self):
        feats = {k: 1.0 for k in FEATURE_KEYS[:-1]}
        assert vectorize(feats) is None


class TestTradeEvents:
    def test_lifecycle_event_types_complete(self):
        for et in ("DecisionCreated", "RiskApproved", "OrderIntentCreated",
                   "BrokerSubmitted", "BrokerAccepted", "BrokerRejected",
                   "PositionOpened", "ProtectionPlaced", "PositionClosed",
                   "FinancialApplied"):
            assert et in EVENT_TYPES

    def test_build_shape(self):
        ev = build("DecisionCreated", user_id="u1", decision_id="d1",
                   account_id="a1", symbol="EURUSD",
                   payload={"verdict": "rejected"})
        assert ev["event_type"] == "DecisionCreated"
        assert ev["schema_v"] == 1
        assert ev["user_id"] == "u1" and ev["decision_id"] == "d1"
        assert ev["account_id"] == "a1" and ev["symbol"] == "EURUSD"
        assert ev["payload"] == {"verdict": "rejected"}
        assert len(ev["event_id"]) == 32
        assert ev["ts_ms"] > 0 and ev["occurred_at"]

    def test_build_rejects_unknown_type(self):
        with pytest.raises(ValueError):
            build("SomethingElse")


class TestBridgeSymbolSpecTradeMode:
    def test_trade_mode_optional_backward_compatible(self):
        spec = BridgeSymbolSpec(point=0.00001, digits=5,
                                stops_level_points=10, freeze_level_points=0)
        assert spec.trade_mode is None            # EA v1.48 payloads still valid

    def test_trade_mode_parsed(self):
        spec = BridgeSymbolSpec(point=0.00001, digits=5,
                                stops_level_points=10, freeze_level_points=0,
                                trade_mode=4)
        assert spec.model_dump()["trade_mode"] == 4


class TestDecisionSchemaStamp:
    def test_engine_stamps_feature_schema_version(self):
        import inspect
        from scalp import engine
        src = inspect.getsource(engine)
        assert '"feature_schema_version": FEATURE_SCHEMA_VERSION' in src

    def test_model_artifact_stamps_and_gates_schema(self):
        import inspect
        from scalp import model as m
        src = inspect.getsource(m)
        assert '"feature_schema_version": FEATURE_SCHEMA_VERSION' in src
        assert 'feature_schema_version' in inspect.getsource(m.load_persisted)
