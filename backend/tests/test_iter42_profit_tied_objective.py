"""iter-42 · Profit-tied learning objective — unit tests.

Every learner (auto-tune, strategy optimizer, adaptive risk, meta-classifier)
must optimize win rate AND profit together. These tests prove that a
high-win-rate-but-losing configuration can no longer be selected or boosted.
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from objective import expectancy_stats, stoic_score  # noqa: E402
from strategy_optimizer import _score  # noqa: E402
from auto_tune import _compute_threshold, MIN_SAMPLES, TARGET_WIN_RATE  # noqa: E402
from adaptive_mode import _profit_tied_multiplier, _multiplier_for  # noqa: E402
from learned_meta import _train_logreg  # noqa: E402


class TestExpectancyStats:
    def test_high_wr_big_losers_is_negative_edge(self):
        # 90% WR, $10 wins, one -$200 loss → expectancy NEGATIVE
        pnls = [10] * 9 + [-200]
        s = expectancy_stats(pnls)
        assert s["win_rate"] == 90.0
        assert s["expectancy_r"] < 0
        assert s["total_pnl"] < 0

    def test_healthy_edge_positive(self):
        pnls = [100] * 6 + [-80] * 4          # 60% WR, payoff 1.25
        s = expectancy_stats(pnls)
        assert s["expectancy_r"] > 0
        assert s["payoff_ratio"] == 1.25

    def test_no_losses(self):
        s = expectancy_stats([50, 60])
        assert s["expectancy_r"] == 1.0 and s["payoff_ratio"] is None


class TestStoicScore:
    def test_losing_variant_negative_despite_high_wr(self):
        assert stoic_score(90.0, -500.0, 40) < 0

    def test_profit_and_wr_both_raise_score(self):
        base = stoic_score(60.0, 1000.0, 30)
        assert stoic_score(70.0, 1000.0, 30) > base   # more WR, same profit
        assert stoic_score(60.0, 2000.0, 30) > base   # more profit, same WR

    def test_strategy_optimizer_uses_it(self):
        winning = {"win_rate": 55.0, "total_pnl_usd": 900.0, "matched_trades": 30}
        wr_trap = {"win_rate": 92.0, "total_pnl_usd": -300.0, "matched_trades": 30}
        assert _score(winning) > _score(wr_trap)
        assert _score(wr_trap) < 0


class TestAutoTuneProfitTie:
    def _rows(self, bucket_conf, pnls):
        return [{"pnl": p, "confidence": bucket_conf} for p in pnls]

    def test_losing_bucket_no_longer_qualifies(self):
        # 70-bucket: 5/6 wins (83% WR) but net-losing (payoff trap)
        rows = self._rows(72, [10, 10, 10, 10, 10, -200])
        res = _compute_threshold(rows, profile_min=65.0)
        assert res["suggested_threshold"] is None
        assert res["source"] == "profile_default"

    def test_profitable_bucket_qualifies(self):
        rows = self._rows(72, [100, 100, 100, 100, -80, -80])  # 66.7% WR, profitable
        assert len(rows) >= MIN_SAMPLES
        res = _compute_threshold(rows, profile_min=65.0)
        assert res["suggested_threshold"] == 70
        assert res["source"] == "auto_tuned"
        entry = next(b for b in res["breakdown"] if b["bucket"] == 70)
        assert entry["expectancy_r"] > 0 and entry["win_rate"] >= TARGET_WIN_RATE


class TestAdaptiveRiskProfitTie:
    def test_hot_streak_without_profit_gets_no_boost(self):
        base = _multiplier_for(75.0)
        assert base > 1.0  # sanity: 75% WR normally boosts
        assert _profit_tied_multiplier(75.0, expectancy_r=-0.2, avg_pnl=5.0) == 1.0

    def test_marginal_edge_stays_neutral(self):
        # 88.9% WR but payoff 0.22 → expectancy 0.084R: no boost either
        assert _profit_tied_multiplier(88.9, expectancy_r=0.084, avg_pnl=8.0) == 1.0

    def test_net_losing_window_forces_risk_down(self):
        assert _profit_tied_multiplier(75.0, expectancy_r=-0.2, avg_pnl=-12.0) <= 0.7

    def test_genuine_edge_keeps_boost(self):
        base = _multiplier_for(75.0)
        assert _profit_tied_multiplier(75.0, expectancy_r=0.4, avg_pnl=30.0) == base

    def test_low_wr_unchanged(self):
        assert _profit_tied_multiplier(35.0, expectancy_r=-0.5, avg_pnl=-20.0) \
            == min(_multiplier_for(35.0), 0.7)


class TestProfitWeightedTraining:
    def test_weights_shift_the_model(self):
        rng = np.random.default_rng(7)
        X = rng.normal(size=(60, 8))
        y = (X[:, 0] > 0).astype(float)
        w_uniform, _, _, _ = _train_logreg(X, y)
        big = np.ones(60)
        big[y == 0] = 4.0   # losses weighted 4x (big losers)
        w_weighted, _, _, _ = _train_logreg(X, y, big)
        assert not np.allclose(w_uniform, w_weighted), \
            "profit weights must influence the learned model"

    def test_backwards_compatible_without_weights(self):
        X = np.random.default_rng(1).normal(size=(30, 8))
        y = (X[:, 0] > 0).astype(float)
        w, mu, sd, auc = _train_logreg(X, y)
        assert w.shape == (9,) and 0 <= auc <= 1


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.unit
