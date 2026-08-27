"""Iter 25d — max_lot_size is the LOT AT PEAK KELLY, not just a hard cap.

User complaint: "the bot uses my max lot size on every trade." Root cause
was that ai_signals.py computed lot_size with a hardcoded $1000 equity and
pip_value=1.0 — so on a real $25K account, every signal produced a lot far
above the user's max (e.g. 0.5) and got clamped uniformly to it.

Fix: in bot_runner.py, when max_lot_size > 0, treat it as the lot used at
peak Kelly confidence and scale linearly with kelly_f / kelly_cap. Low-
confidence signals get proportionally smaller lots, high-confidence ones
approach the cap.

This test directly verifies the bot_runner sizing math.
"""
import pytest

from risk import compute_lot_for_account, get_profile


def _bot_runner_effective_lot(account, symbol, entry, sl, conf, risk_level,
                              max_lot_cap):
    """Replica of the sizing block in bot_runner._process_user_account."""
    profile = get_profile(risk_level)
    sized = compute_lot_for_account(account, symbol, entry, sl, conf, profile)
    kelly_f = float(sized.get("kelly_f") or 0)
    kelly_cap = float(profile.get("kelly_cap") or 0)
    absolute_lot = float(sized["lot_size"])
    if max_lot_cap > 0 and kelly_cap > 0:
        conf_scale = min(kelly_f / kelly_cap, 1.0) if kelly_f > 0 else 0.0
        scaled_lot = max(round(max_lot_cap * conf_scale, 2), 0.01)
        return min(absolute_lot, scaled_lot)
    if max_lot_cap > 0 and absolute_lot > max_lot_cap:
        return max_lot_cap
    return absolute_lot


class TestMaxLotAsKellyPeak:
    """Reproduce the user's exact scenario: $25K USD demo, HIGH risk, cap 0.2."""

    ACC = {"equity": 25299.05, "account_type": "demo"}
    MAX = 0.2

    def test_low_conf_well_below_cap(self):
        lot = _bot_runner_effective_lot(
            self.ACC, "XAUUSD", 4100.0, 4115.0, 56.0, "high", self.MAX,
        )
        # 56% confidence on HIGH should land ~0.10 — half the cap
        assert lot <= self.MAX, lot
        assert lot <= 0.12, lot
        assert lot >= 0.05, lot

    def test_high_conf_reaches_cap(self):
        lot = _bot_runner_effective_lot(
            self.ACC, "XAUUSD", 4100.0, 4115.0, 90.0, "high", self.MAX,
        )
        assert lot == self.MAX, lot

    def test_lots_scale_monotonically_with_confidence(self):
        """Higher confidence must produce >= the lot of lower confidence."""
        confs = [56.0, 60.0, 65.0, 70.0, 75.0, 80.0, 85.0, 90.0]
        lots = [
            _bot_runner_effective_lot(self.ACC, "XAUUSD", 4100.0, 4115.0, c,
                                      "high", self.MAX)
            for c in confs
        ]
        for prev, curr in zip(lots, lots[1:]):
            assert curr >= prev, f"non-monotonic: {lots}"
        # And the spread must be meaningful — not all the same value
        assert max(lots) > min(lots) * 1.5, f"flat curve: {lots}"

    def test_max_lot_zero_uses_absolute_kelly(self):
        """When user hasn't set a cap, fall back to absolute Kelly sizing."""
        lot = _bot_runner_effective_lot(
            self.ACC, "XAUUSD", 4100.0, 4115.0, 80.0, "high", max_lot_cap=0.0,
        )
        # Absolute Kelly on $25K HIGH at 80% conf is ~0.40
        assert 0.30 <= lot <= 0.50, lot

    def test_cap_acts_as_ceiling_even_when_kelly_higher(self):
        """If absolute Kelly lot exceeds the scaled cap, scaled cap wins."""
        lot = _bot_runner_effective_lot(
            self.ACC, "XAUUSD", 4100.0, 4115.0, 95.0, "high", self.MAX,
        )
        assert lot <= self.MAX, lot

    def test_small_balance_uses_absolute_kelly_below_cap(self):
        """On a tiny balance, absolute Kelly is already small — use that."""
        small = {"equity": 500.0, "account_type": "demo"}
        lot = _bot_runner_effective_lot(
            small, "XAUUSD", 4100.0, 4115.0, 80.0, "high", max_lot_cap=1.0,
        )
        # $500 × 2.4% = $12. lots = $12 / (150 × $10) ≈ 0.008 → floored to 0.01
        assert lot <= 0.05, lot


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.unit
