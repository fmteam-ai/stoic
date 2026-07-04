"""iter-41 · Payoff-ratio repair — unit tests.

Covers: Soft-Stop firing rules (trade_manager), payoff stats (ai_optimizer),
new optimizer-recommendable fields, model defaults, and the VaR-cap
worst-loser preference.
"""
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trade_manager import _soft_stop_should_fire  # noqa: E402
from ai_optimizer import compute_trade_stats, validate_recommendations, ALLOWED_FIELDS, _clamp  # noqa: E402
from models import BotConfigUpdate  # noqa: E402


def _old_iso(minutes=60):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


class TestSoftStop:
    CFG = {"soft_stop_enabled": True, "soft_stop_loss_fraction": 0.6,
           "soft_stop_min_minutes": 10}

    def test_disabled_never_fires(self):
        assert not _soft_stop_should_fire({}, -200, 150, _old_iso())
        assert not _soft_stop_should_fire({"soft_stop_enabled": False}, -200, 150, _old_iso())

    def test_profit_never_fires(self):
        assert not _soft_stop_should_fire(self.CFG, 50, 150, _old_iso())
        assert not _soft_stop_should_fire(self.CFG, 0, 150, _old_iso())

    def test_fires_at_fraction_of_sl(self):
        # 60% of 150 pips = 90 pips adverse
        assert _soft_stop_should_fire(self.CFG, -90, 150, _old_iso())
        assert _soft_stop_should_fire(self.CFG, -149, 150, _old_iso())
        assert not _soft_stop_should_fire(self.CFG, -89, 150, _old_iso())

    def test_min_age_gate(self):
        young = _old_iso(minutes=5)   # younger than 10-min gate
        assert not _soft_stop_should_fire(self.CFG, -120, 150, young)
        assert _soft_stop_should_fire(self.CFG, -120, 150, _old_iso(minutes=11))

    def test_fraction_clamped(self):
        cfg = {**self.CFG, "soft_stop_loss_fraction": 0.1}  # clamps to 0.3
        assert _soft_stop_should_fire(cfg, -45, 150, _old_iso())      # 30% of 150
        cfg = {**self.CFG, "soft_stop_loss_fraction": 5.0}  # clamps to 0.9
        assert not _soft_stop_should_fire(cfg, -100, 150, _old_iso())  # needs 135

    def test_unknown_age_does_not_block(self):
        assert _soft_stop_should_fire(self.CFG, -90, 150, None)

    def test_zero_sl_never_fires(self):
        assert not _soft_stop_should_fire(self.CFG, -90, 0, _old_iso())


class TestPayoffStats:
    def test_payoff_ratio_and_expectancy(self):
        trades = [{"pnl": 50}, {"pnl": 50}, {"pnl": 50}, {"pnl": -150}]
        s = compute_trade_stats(trades)
        assert s["win_rate"] == 75.0
        assert s["payoff_ratio"] == round(50 / 150, 2)   # 0.33 — the trap
        assert s["avg_pnl_per_trade"] == 0.0             # flat despite 75% WR

    def test_payoff_none_without_losses(self):
        s = compute_trade_stats([{"pnl": 10}, {"pnl": 20}])
        assert s["payoff_ratio"] is None


class TestOptimizerNewFields:
    def test_new_fields_whitelisted(self):
        for f in ("soft_stop_enabled", "soft_stop_loss_fraction",
                  "soft_stop_min_minutes", "let_winners_run"):
            assert f in ALLOWED_FIELDS

    def test_clamps(self):
        assert _clamp("soft_stop_loss_fraction", 0.95) == 0.9
        assert _clamp("soft_stop_loss_fraction", 0.1) == 0.3
        assert _clamp("soft_stop_min_minutes", 500) == 120
        assert _clamp("let_winners_run", 1) is True

    def test_recommendation_flow(self):
        cfg = {"soft_stop_enabled": False, "let_winners_run": False, "active": True}
        recs = validate_recommendations([
            {"type": "config_change", "field": "soft_stop_enabled", "to": True, "reason": "r"},
            {"type": "config_change", "field": "let_winners_run", "to": True, "reason": "r"},
        ], cfg)
        assert [r["field"] for r in recs] == ["soft_stop_enabled", "let_winners_run"]
        assert all(r["to"] is True and r["from"] is False for r in recs)


class TestModelDefaults:
    def test_defaults_conservative(self):
        m = BotConfigUpdate()
        assert m.soft_stop_enabled is False       # off by default
        assert m.soft_stop_loss_fraction == 0.6
        assert m.soft_stop_min_minutes == 10
        assert m.let_winners_run is False


def test_var_cap_prefers_worst_loser_source():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "portfolio", "risk_manager.py")).read()
    seg = src[src.index('triggers.append("var_cap")'):]
    assert "live_pnl" in seg[:1200], "var_cap must prefer culling the worst loser"
