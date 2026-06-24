"""Iter 25c — Account-aware Kelly position sizing.

Regression: `compute_lot_for_account` must use the real account equity and
proper pip math so that the user's `max_lot_size` cap is a CEILING, not a
permanently-binding default. Low-confidence signals must yield lots well
below the cap; high-confidence signals may approach or exceed the cap (in
which case the runner clamps them down).
"""
import pytest

from risk import compute_lot_for_account, get_profile


HIGH = get_profile("high")
MEDIUM = get_profile("medium")
LOW = get_profile("low")


class TestAccountAwareLotSizing:
    def test_zero_equity_returns_minimum_lot(self):
        out = compute_lot_for_account(
            account={"balance": 0, "equity": 0, "account_type": "standard"},
            symbol="XAUUSD", entry_price=4100.0, stop_loss=4115.0,
            confidence_pct=70.0, profile=HIGH,
        )
        assert out["lot_size"] == 0.01
        assert out["method"] == "fallback_no_equity"

    def test_zero_sl_returns_minimum_lot(self):
        out = compute_lot_for_account(
            account={"equity": 1000.0, "account_type": "standard"},
            symbol="XAUUSD", entry_price=4100.0, stop_loss=4100.0,
            confidence_pct=70.0, profile=HIGH,
        )
        assert out["lot_size"] == 0.01
        assert out["method"] == "fallback_zero_sl"

    def test_xau_25k_high_conf_high_profile_below_old_kelly(self):
        """Real $25K USD account, HIGH profile, 70% confidence, 15 USD SL.
        Old (broken) Kelly with $1000 fake equity produced ~0.8 lots.
        New math should land much smaller in absolute lot units."""
        out = compute_lot_for_account(
            account={"equity": 25000.0, "account_type": "standard"},
            symbol="XAUUSD", entry_price=4100.0, stop_loss=4115.0,
            confidence_pct=70.0, profile=HIGH,
        )
        # sl_pips = 150, pip_usd = $10/lot, risk_amount = $25K × ~1.9% = ~$483
        # lots = $483 / (150 × $10) = ~0.32
        assert out["method"] == "kelly_account_aware"
        assert 0.20 <= out["lot_size"] <= 0.50, out
        assert out["equity"] == 25000.0
        assert out["sl_pips"] == 150.0
        assert out["pip_usd_per_lot"] == 10.0

    def test_xau_25k_low_conf_yields_lot_well_below_cap(self):
        """With 56% confidence on HIGH profile (min_conf=55), Kelly should
        produce a much smaller lot — proving the cap doesn't always bind."""
        out = compute_lot_for_account(
            account={"equity": 25000.0, "account_type": "standard"},
            symbol="XAUUSD", entry_price=4100.0, stop_loss=4115.0,
            confidence_pct=56.0, profile=HIGH,
        )
        # Lower kelly_f at 56% conf → smaller lot
        assert 0.05 <= out["lot_size"] <= 0.25, out
        # And critically: less than what 70% conf produces
        out70 = compute_lot_for_account(
            account={"equity": 25000.0, "account_type": "standard"},
            symbol="XAUUSD", entry_price=4100.0, stop_loss=4115.0,
            confidence_pct=70.0, profile=HIGH,
        )
        assert out["lot_size"] < out70["lot_size"], (out, out70)

    def test_microcent_account_scales_lot_up_x1000(self):
        """A microcent account moves the decimal — same USD risk → 1000×
        the MT5 lot quantity vs a standard account."""
        std = compute_lot_for_account(
            account={"equity": 1000.0, "account_type": "standard"},
            symbol="XAUUSD", entry_price=4100.0, stop_loss=4115.0,
            confidence_pct=70.0, profile=MEDIUM,
        )
        micro = compute_lot_for_account(
            account={"equity": 1000.0, "account_type": "microcent"},
            symbol="XAUUSD", entry_price=4100.0, stop_loss=4115.0,
            confidence_pct=70.0, profile=MEDIUM,
        )
        # Microcent lot quantity should be ~1000× the standard lot quantity
        # (since the lot multiplier is 0.001 → pip_value is /1000 → lot is ×1000).
        assert micro["lot_size"] > 100 * std["lot_size"], (std, micro)

    def test_btc_pip_value_one_dollar_per_lot(self):
        out = compute_lot_for_account(
            account={"equity": 10000.0, "account_type": "standard"},
            symbol="BTCUSD", entry_price=62000.0, stop_loss=61500.0,
            confidence_pct=70.0, profile=MEDIUM,
        )
        # sl_pips = 500, pip_usd = $1/lot, risk_amount ≈ $10K × ~0.9% ≈ $90
        # lots ≈ $90 / (500 × $1) = 0.18
        assert out["pip_usd_per_lot"] == 1.0
        assert out["sl_pips"] == 500.0
        assert 0.05 <= out["lot_size"] <= 0.50, out

    def test_below_min_confidence_returns_minimum_lot(self):
        """LOW profile requires 75% confidence; a 60% signal yields f=0 → lot 0.01."""
        out = compute_lot_for_account(
            account={"equity": 10000.0, "account_type": "standard"},
            symbol="XAUUSD", entry_price=4100.0, stop_loss=4115.0,
            confidence_pct=60.0, profile=LOW,
        )
        assert out["lot_size"] == 0.01
        assert out["kelly_f"] == 0.0
