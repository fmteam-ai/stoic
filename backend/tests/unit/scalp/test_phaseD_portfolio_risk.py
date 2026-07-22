"""Phase D — portfolio-level risk: correlation matrix, currency exposure,
stress scenarios, volatility-adjusted allocation, cross-strategy gate.
MongoDB-free unit tests."""
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import inspect

import pytest

import portfolio_risk as pr

pytestmark = pytest.mark.unit


def _pos(symbol, action="BUY", lot=0.10, entry=None, stop=None):
    return {"symbol": symbol, "action": action, "lot": lot,
            "entry_price": entry, "stop_loss": stop}


class TestCorrelationMatrix:
    def test_identical_symbol(self):
        assert pr.pair_correlation("EURUSD", "EURUSD") == 1.0

    def test_shared_quote_positive(self):
        assert pr.pair_correlation("EURUSD", "GBPUSD") == 0.65

    def test_usd_side_flip_negative(self):
        assert pr.pair_correlation("EURUSD", "USDCHF") == -0.65

    def test_group_priors(self):
        assert pr.pair_correlation("XAUUSD", "XAGUSD") == 0.85
        assert pr.pair_correlation("US30", "NAS100") == 0.85
        assert pr.pair_correlation("BTCUSD", "ETHUSD") == 0.85

    def test_unrelated_zero(self):
        assert pr.pair_correlation("EURJPY", "XAUUSD") == 0.0

    def test_position_correlation_uses_direction(self):
        # long EURUSD vs short USDCHF = SAME macro bet → positive
        a = _pos("EURUSD", "BUY")
        b = _pos("USDCHF", "SELL")
        assert pr.position_correlation(a, b) == pytest.approx(0.65)
        # long EURUSD vs long GBPUSD = positively correlated
        assert pr.position_correlation(
            _pos("EURUSD"), _pos("GBPUSD")) == pytest.approx(0.65)
        # long vs short the same symbol → -1
        assert pr.position_correlation(
            _pos("EURUSD", "BUY"), _pos("EURUSD", "SELL")) == -1.0


class TestCurrencyExposure:
    def test_leg_decomposition(self):
        assert pr.legs("EURUSD") == ("EUR", "USD")
        assert pr.legs("XAUUSD") == ("XAU", "USD")
        assert pr.legs("US30") == ("US30", "USD")

    def test_net_exposure_aggregates_legs(self):
        # two longs sharing short-USD: USD exposure stacks negatively
        pos = [_pos("EURUSD", "BUY", 1.0, 1.0850, 1.0840),   # $100 risk
               _pos("GBPUSD", "BUY", 1.0, 1.2650, 1.2640)]   # $100 risk
        exp = pr.currency_exposure(pos)
        assert exp["USD"] == pytest.approx(-200.0, abs=1)
        assert exp["EUR"] == pytest.approx(100.0, abs=1)

    def test_opposing_positions_net_out(self):
        pos = [_pos("EURUSD", "BUY", 1.0, 1.0850, 1.0840),
               _pos("EURUSD", "SELL", 1.0, 1.0850, 1.0860)]
        exp = pr.currency_exposure(pos)
        assert exp["USD"] == pytest.approx(0.0, abs=1)


class TestStressScenarios:
    def test_worst_leg_with_gap_multiplier(self):
        pos = [_pos("EURUSD", "BUY", 1.0, 1.0850, 1.0840),   # $100
               _pos("GBPUSD", "BUY", 1.0, 1.2650, 1.2640)]   # $100
        st = pr.stress_loss_usd(pos, gap_mult=2.0)
        # both are short-USD: a USD spike hits $200 of stop risk × 2 gap
        assert st["worst_leg"] == "USD"
        assert st["loss_usd"] == pytest.approx(400.0, abs=2)


class TestVolAdjustedAllocation:
    def test_downscale_only(self):
        assert pr.vol_size_multiplier(2.0, 1.0) == 0.5     # hot → floor
        assert pr.vol_size_multiplier(1.5, 1.0) == pytest.approx(0.667)
        assert pr.vol_size_multiplier(0.5, 1.0) == 1.0     # calm → NO bonus
        assert pr.vol_size_multiplier(None, 1.0) == 1.0
        assert pr.vol_size_multiplier(1.0, 0.0) == 1.0


class TestPortfolioEvaluate:
    EQ = 10_000.0

    def test_clean_portfolio_allows(self):
        out = pr.evaluate([], _pos("EURUSD", "BUY", 0.05, 1.0850, 1.0845),
                          self.EQ)
        assert out["ok"] is True
        assert out["blocks"] == []

    def test_correlated_cluster_blocks(self):
        # $120 open GBPUSD risk × 0.65 corr + $100 candidate ≈ $178
        # cluster vs 1.5% × 10k = $150 cap → block
        open_pos = [_pos("GBPUSD", "BUY", 1.2, 1.2650, 1.2640)]
        out = pr.evaluate(open_pos,
                          _pos("EURUSD", "BUY", 1.0, 1.0850, 1.0840), self.EQ)
        assert out["ok"] is False
        assert any("cluster" in b for b in out["blocks"])

    def test_anticorrelated_position_does_not_count(self):
        # short GBPUSD is negatively correlated with the long EURUSD
        # candidate → no risk stacking in the cluster budget.
        open_pos = [_pos("GBPUSD", "SELL", 1.2, 1.2650, 1.2660)]
        out = pr.evaluate(open_pos,
                          _pos("EURUSD", "BUY", 1.0, 1.0850, 1.0840), self.EQ)
        assert not any("cluster" in b for b in out["blocks"])

    def test_currency_exposure_blocks(self):
        # combined short-USD risk $250 vs 2% × 10k = $200 cap → block
        open_pos = [_pos("EURUSD", "BUY", 1.0, 1.0850, 1.0840),
                    _pos("GBPUSD", "BUY", 0.5, 1.2650, 1.2640),
                    _pos("AUDUSD", "BUY", 0.5, 0.6550, 0.6540)]
        out = pr.evaluate(open_pos,
                          _pos("NZDUSD", "BUY", 0.5, 0.6050, 0.6040),
                          self.EQ, params={"cluster_risk_pct": 99,
                                           "stress_pct": 99})
        assert out["ok"] is False
        assert any("USD" in b for b in out["blocks"])

    def test_stress_scenario_blocks(self):
        # short-USD gross risk $1600 × 2 gap = $3200 vs 5% × 10k = $500
        open_pos = [_pos("EURUSD", "BUY", 5.0, 1.0850, 1.0830),   # $1000
                    _pos("GBPUSD", "BUY", 3.0, 1.2650, 1.2630)]   # $600
        out = pr.evaluate(open_pos,
                          _pos("AUDUSD", "BUY", 1.0, 0.6550, 0.6530),
                          self.EQ, params={"cluster_risk_pct": 99,
                                           "ccy_risk_pct": 99})
        assert out["ok"] is False
        assert any("stress" in b for b in out["blocks"])

    def test_explainability_payload(self):
        out = pr.evaluate([_pos("GBPUSD", "BUY", 0.1, 1.2650, 1.2640)],
                          _pos("EURUSD", "BUY", 0.05, 1.0850, 1.0845),
                          self.EQ)
        for key in ("candidate_risk_usd", "cluster_risk_usd",
                    "currency_exposure", "stress", "correlations"):
            assert key in out
        assert "EURUSD~GBPUSD" in out["correlations"] or \
            "GBPUSD~EURUSD" in out["correlations"]


class TestPhaseDWiring:
    def test_engine_portfolio_gate(self):
        from scalp import engine
        src = inspect.getsource(engine)
        assert "portfolio_risk.open_positions(" in src
        assert "portfolio_risk.evaluate(" in src
        assert '"portfolio_risk"' in src            # reject stage
        assert "vol_size_multiplier(" in src
        assert '"vol_size_mult"' in src

    def test_gate_is_cross_strategy(self):
        # the open-positions query must span EVERY scope, not just scalp
        src = inspect.getsource(pr.open_positions)
        assert "scope" not in str(src.split("find(")[1].split(",")[0])

    def test_main_snapshot_integrates_phase_d(self):
        src = open(_os.path.join(_BACKEND_DIR, "portfolio/risk_manager.py")).read()
        assert "_pr.currency_exposure(" in src
        assert "_pr.stress_loss_usd(" in src
        assert "position_correlations" in src
