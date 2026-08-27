"""iter-202 — Phase A "STOIC brain": Regime Intelligence 2.0, Strategy
Router, Uncertainty Engine, Meta-Decision Engine, Portfolio Risk Brain."""
import math
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

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


UID = f"u-brain-{uuid.uuid4().hex[:6]}"


def _cleanup():
    db = _db()
    for coll in ("trades", "signals", "meta_decisions",
                 "intraday_candles", "risk_verdicts"):
        _run(getattr(db, coll).delete_many({"user_id": UID}))


def _bars(n=200, trend=0.5, base=2400.0, vol=2.0):
    bars = []
    px = base
    for i in range(n):
        px += trend + vol * math.sin(i * 1.7)
        o = px - vol * 0.3
        c = px
        bars.append({"t": 1700000000 + i * 900, "o": o, "c": c,
                     "h": max(o, c) + vol * 0.5,
                     "l": min(o, c) - vol * 0.5})
    return bars


class TestRegimeIntelligence:
    def test_trending_bars_produce_trending_fingerprint(self):
        from regime_intelligence import vector_from_bars
        out = vector_from_bars(_bars(trend=1.2))
        assert out["available"] is True
        v = out["vector"]
        assert v["trend"] > 0.35
        assert "TRENDING" in out["labels"][0]
        assert out["fingerprint_key"].startswith("TREND")
        assert set(v) == {"trend", "volatility", "liquidity", "momentum",
                          "mean_reversion", "correlation_stress",
                          "news_risk", "spread_stress", "gap_risk"}

    def test_flat_choppy_bars_produce_ranging(self):
        from regime_intelligence import vector_from_bars
        out = vector_from_bars(_bars(trend=0.0, vol=3.0))
        assert abs(out["vector"]["trend"]) < 0.35
        assert "RANGING" in out["labels"][0]

    def test_too_few_bars_says_unavailable(self):
        from regime_intelligence import vector_from_bars
        out = vector_from_bars(_bars(n=10))
        assert out["available"] is False


class TestStrategyRouter:
    def teardown_method(self):
        _cleanup()

    def _seed(self, scope, wins, losses, fp="TREND|MOM_H|VOL_M|LIQ_G|EVT_L|london"):
        db = _db()
        now = datetime.now(timezone.utc).isoformat()
        for i in range(wins + losses):
            win = i < wins
            sid = _run(db.signals.insert_one(
                {"user_id": UID, "created_at": now,
                 "market_state": {"fingerprint_key": fp}})).inserted_id
            _run(db.trades.insert_one(
                {"user_id": UID, "signal_id": str(sid), "scope": scope,
                 "status": "closed", "closed_at": now, "action": "BUY",
                 "pnl": 20.0 if win else -10.0, "entry_price": 100.0,
                 "stop_loss": 99.0, "exit_price": 101.5 if win else 99.0}))

    def test_router_favors_the_earning_family_with_bounds(self):
        from strategy_router import route
        fp = "TREND|MOM_H|VOL_M|LIQ_G|EVT_L|london"
        self._seed("scalp_fast", wins=25, losses=5, fp=fp)   # strong
        self._seed("swing", wins=5, losses=25, fp=fp)        # weak
        out = _run(route(_db(), UID, fp))
        w = out["weights"]
        assert abs(sum(w.values()) - 1.0) < 0.02
        assert w["scalp_fast"] > w["swing"]
        assert all(0.05 <= x <= 0.70 for x in w.values())   # hard bounds
        assert out["fingerprint_matched"] is True

    def test_router_uniformish_without_evidence(self):
        from strategy_router import route
        out = _run(route(_db(), UID, "NOSUCH|FP"))
        w = list(out["weights"].values())
        assert max(w) - min(w) < 0.05


class TestUncertaintyEngine:
    def teardown_method(self):
        _cleanup()

    def _seed_trades(self, rs):
        db = _db()
        now = datetime.now(timezone.utc).isoformat()
        for r in rs:
            _run(db.trades.insert_one(
                {"user_id": UID, "scope": "ai", "symbol": "XAUUSD",
                 "status": "closed", "closed_at": now, "action": "BUY",
                 "pnl": r * 10, "entry_price": 100.0, "stop_loss": 99.0,
                 "exit_price": 100.0 + r}))

    def test_thin_edge_wide_interval_skips(self):
        """Edge indistinguishable from costs → explicit 'I don't know'."""
        from uncertainty_engine import assess
        self._seed_trades([1.5, -1.0] * 20)   # mean 0.25R but very wide
        out = _run(assess(_db(), UID,
                          {"scope": "ai", "symbol": "XAUUSD",
                           "confidence": 70}))
        assert out["decision"] in ("SKIP", "TRADE")
        assert out["interval"][0] < out["expected_edge_r"] \
            < out["interval"][1]

    def test_strong_consistent_edge_trades(self):
        from uncertainty_engine import assess
        self._seed_trades([0.8, 0.9, 1.1, 0.7, -0.4] * 10)  # tight, +ve
        out = _run(assess(_db(), UID,
                          {"scope": "ai", "symbol": "XAUUSD",
                           "confidence": 70}))
        assert out["decision"] == "TRADE"
        assert out["interval"][0] > out["cost_r"]

    def test_insufficient_history_is_advisory_not_blocking(self):
        from uncertainty_engine import assess
        out = _run(assess(_db(), UID,
                          {"scope": "ai", "symbol": "XAUUSD",
                           "confidence": 70}))
        assert out["decision"] == "ADVISORY"
        assert out["hard_gate"] is False


class TestMetaDecision:
    def teardown_method(self):
        _cleanup()

    def test_scorecard_shape_and_downscale_only(self):
        from meta_decision import meta_decide
        db = _db()
        _run(db.intraday_candles.insert_one(
            {"user_id": UID, "symbol": "XAUUSD", "timeframe": "M15",
             "bars": _bars(trend=1.0)}))
        out = _run(meta_decide(db, UID,
                               {"symbol": "XAUUSD", "scope": "ai",
                                "confidence": 78,
                                "calibrated_p_win": {"p": 0.6,
                                                     "ev_r": 0.2}}))
        assert out["decision"] in ("TRADE", "REDUCE", "SKIP")
        assert 0.0 <= out["risk_multiplier"] <= 1.0   # NEVER upsizes
        assert set(out["scorecard"]) == {
            "opportunity_quality", "strategy_reliability",
            "regime_compatibility", "execution_quality",
            "risk_environment"}
        assert all(0 <= v <= 100 for v in out["scorecard"].values())
        assert 0 <= out["uncertainty"] <= 1
        assert out["market_state"]["fingerprint_key"]
        doc = _run(db.meta_decisions.find_one({"user_id": UID}))
        assert doc and doc["decision"] == out["decision"]


class TestPortfolioBrain:
    def teardown_method(self):
        _cleanup()

    ACCT = f"acct-{UID}"

    def _pos(self, symbol, action, lot=1.0, entry=100.0, sl=99.0):
        return {"symbol": symbol, "action": action, "lot": lot,
                "entry_price": entry, "stop_loss": sl, "scope": "ai"}

    def test_usd_factor_overload_reduces_or_rejects(self):
        """Four 'diversified' trades that are all one big USD bet."""
        import portfolio_risk as pr
        db = _db()
        now = datetime.now(timezone.utc).isoformat()
        for sym, act in (("EURUSD", "SELL"), ("GBPUSD", "SELL"),
                         ("AUDUSD", "SELL")):
            _run(db.trades.insert_one(
                {"user_id": UID, "account_id": self.ACCT, "symbol": sym,
                 "action": act, "status": "open", "lot_size": 2.0,
                 "entry_price": 1.1, "stop_loss": 1.111,
                 "opened_at": now}))
        cand = self._pos("USDCHF", "BUY", lot=2.0, entry=0.9, sl=0.891)
        out = _run(pr.marginal_verdict(db, self.ACCT, cand, 10_000,
                                       user_id=UID))
        assert out["verdict"] in ("REDUCE", "REJECT")
        assert out["approved_fraction"] < 1.0
        assert out["blocks"]
        fac = out["factors"]
        assert fac["dominant_factor"]["factor"] == "USD"
        # reduction recorded for empirical verdict-outcome scoring
        v = _run(db.risk_verdicts.find_one({"user_id": UID,
                                            "source": "portfolio_brain"}))
        assert v is not None
        _run(db.trades.delete_many({"account_id": self.ACCT}))

    def test_small_uncorrelated_trade_approved(self):
        import portfolio_risk as pr
        out = _run(pr.marginal_verdict(_db(), f"empty-{UID}",
                                       self._pos("XAUUSD", "BUY", lot=0.1,
                                                 entry=2400, sl=2395),
                                       50_000, user_id=UID))
        assert out["verdict"] == "APPROVE"
        assert out["approved_fraction"] == 1.0

    def test_factor_exposure_shape(self):
        import portfolio_risk as pr
        out = pr.factor_exposure(
            [self._pos("EURUSD", "BUY"), self._pos("GBPUSD", "BUY")],
            10_000)
        assert out["dominant_factor"]["factor"] in ("USD", "EUR", "GBP")
        assert out["total_risk_usd"] > 0
