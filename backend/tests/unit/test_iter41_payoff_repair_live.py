"""iter-41 payoff repair — live-code tests restored in v1.60.50 (the soft-stop part was dropped with the dead knob)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from ai_optimizer import compute_trade_stats, validate_recommendations, ALLOWED_FIELDS, _clamp  # noqa: E402
from models import BotConfigUpdate  # noqa: E402

pytestmark = pytest.mark.unit
BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestPayoffStats:
    def test_payoff_ratio_and_expectancy(self):
        trades = [{"pnl": 50}, {"pnl": 50}, {"pnl": 50}, {"pnl": -150}]
        s = compute_trade_stats(trades)
        assert s["win_rate"] == 75.0
        assert s["payoff_ratio"] == round(50 / 150, 2)   # 0.33 — the trap
        assert s["avg_pnl_per_trade"] == 0.0             # flat despite 75% WR

    def test_payoff_none_without_losses(self):
        assert compute_trade_stats([{"pnl": 10}, {"pnl": 20}])["payoff_ratio"] is None


class TestOptimizerFields:
    def test_let_winners_run_whitelisted_and_soft_stop_gone(self):
        assert "let_winners_run" in ALLOWED_FIELDS
        assert not any(f.startswith("soft_stop") for f in ALLOWED_FIELDS)

    def test_clamps(self):
        assert _clamp("let_winners_run", 1) is True
        assert _clamp("max_concurrent_trades", 99) == ALLOWED_FIELDS["max_concurrent_trades"][2]

    def test_recommendation_flow(self):
        cfg = {"let_winners_run": False, "active": True}
        recs = validate_recommendations([
            {"type": "config_change", "field": "let_winners_run", "to": True, "reason": "r"},
            {"type": "config_change", "field": "soft_stop_enabled", "to": True, "reason": "dead knob"},
        ], cfg)
        assert [r["field"] for r in recs] == ["let_winners_run"]
        assert recs[0]["to"] is True and recs[0]["from"] is False


def test_model_default_let_winners_run_off():
    assert BotConfigUpdate().let_winners_run is False


def test_var_cap_prefers_worst_loser_source():
    src = open(os.path.join(BACKEND, "portfolio", "risk_manager.py")).read()
    seg = src[src.index('triggers.append("var_cap")'):]
    assert "live_pnl" in seg[:1200], "var_cap must prefer culling the worst loser"
