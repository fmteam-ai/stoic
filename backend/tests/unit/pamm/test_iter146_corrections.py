"""iter-146 corrections — unit proofs for:
1) signed factor exposure (multi-strategy netting model)
2) minimum slippage evidence sample counts for latency-critical styles
"""
import pytest

from modules.pamm.strategy_guard import (MIN_SLIPPAGE_SAMPLES,
                                         signed_factor_lots,
                                         slippage_evidence_violation)

pytestmark = pytest.mark.unit


class TestSignedFactorLots:
    def test_buy_long_base_short_quote(self):
        assert signed_factor_lots("EURUSD", "BUY", 1.0) == {
            "EUR": 1.0, "USD": -1.0}

    def test_sell_flips_sign(self):
        assert signed_factor_lots("XAUUSD", "SELL", 0.5) == {
            "XAU": -0.5, "USD": 0.5}

    def test_missing_action_treated_as_buy(self):
        assert signed_factor_lots("GBPUSD", None, 2.0) == {
            "GBP": 2.0, "USD": -2.0}

    def test_non_fx_symbol_yields_partial_or_empty(self):
        assert signed_factor_lots("US30", "BUY", 1.0) == {}
        assert signed_factor_lots("", "BUY", 1.0) == {}


class TestSlippageEvidenceMinimums:
    def test_thresholds(self):
        assert MIN_SLIPPAGE_SAMPLES == {"VERY_HIGH": 10, "MAXIMUM": 20}

    def test_nitro_blocked_below_minimum(self):
        v = slippage_evidence_violation(
            "MAXIMUM", {"recent_slippage_sample_count": 19})
        assert v and v["reason"] == "insufficient_slippage_evidence"
        assert v["detail"]["required"] == 20

    def test_fast_scalp_blocked_below_minimum(self):
        v = slippage_evidence_violation(
            "VERY_HIGH", {"recent_slippage_sample_count": 9})
        assert v and v["detail"]["required"] == 10

    def test_enough_samples_pass(self):
        assert slippage_evidence_violation(
            "MAXIMUM", {"recent_slippage_sample_count": 20}) is None
        assert slippage_evidence_violation(
            "VERY_HIGH", {"recent_slippage_sample_count": 10}) is None

    def test_low_sensitivity_styles_unaffected(self):
        for sens in ("LOW", "MEDIUM", "MEDIUM_HIGH", "HIGH", None, ""):
            assert slippage_evidence_violation(sens, {}) is None

    def test_missing_count_counts_as_zero(self):
        v = slippage_evidence_violation("MAXIMUM", {})
        assert v and v["detail"]["sample_count"] == 0
