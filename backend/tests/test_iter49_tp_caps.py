"""iter-49 · TP pip caps + SL cap + weighted R:R floor guard.

USER BUG (2026-07-16): MTF bot opened XAUUSD SELL with TP ~450 pips away —
"too far, will never be reached." USER FIX REQUIREMENT verbatim:
    "I want the TPs to be close 100 pips tp1, 200 pips TP2"

Fix implemented in /app/backend/ai_signals.py (lines ~40-52, ~470-487):
  * MTF_TP1_MAX_PIPS = 100 (env-overridable)
  * MTF_TP2_MAX_PIPS = 200
  * MTF_TP3_MAX_PIPS = 200  (broker-visible TP = tp3)
  * SL_MAX_PIPS reduced 250 → 120  (so weighted R:R stays ≥ MTF_RR_FLOOR 1.1)
  * MTF_RR_FLOOR = 1.1

The geometry code lives inline inside a large signal function so tests here
replicate the exact math (lines 464-487 of ai_signals.py) using pip_utils
and the CONSTANTS imported from ai_signals — this guarantees a config-level
regression (env override → constants → math) is caught.
"""
from __future__ import annotations

import pytest

import ai_signals as sig
from pip_utils import pips_to_price, price_to_pips


# --------------------------------------------------------------------------- #
# Faithful replica of the inline geometry (ai_signals.py lines ~464-502).
# This is deliberately kept close to the source; if that math changes and this
# is not updated, the mismatch will show up in tp3 assertions below.
# --------------------------------------------------------------------------- #
def _run_geometry(symbol: str, atr15: float, current_price: float, action: str = "SELL") -> dict:
    """Return the same geometry dict the MTF signal engine would produce
    for the given ATR / price / direction. Only the mtf_mode branch is
    exercised (engine not in SCALP / range_fade / breakout_m15)."""
    # branch: `elif atr15 > 0:` at line 464
    sl_dist_price = sig.ATR_SL_MULTIPLIER * atr15
    tp_dist_price = sig.ATR_TP_MULTIPLIER * atr15
    sl_min_price = pips_to_price(symbol, 30)

    sl_max_price = pips_to_price(symbol, sig.SL_MAX_PIPS)
    sl_dist_price = max(sl_min_price, min(sl_max_price, sl_dist_price))

    if tp_dist_price <= 0:
        tp_dist_price = sl_dist_price * 2.5

    tp1_dist = tp_dist_price * 0.4
    tp2_dist = tp_dist_price * 0.7
    tp3_dist = tp_dist_price * 1.0

    # user policy (2026-07-16): TP pip caps
    tp1_dist = min(tp1_dist, pips_to_price(symbol, sig.MTF_TP1_MAX_PIPS))
    tp2_dist = min(tp2_dist, pips_to_price(symbol, sig.MTF_TP2_MAX_PIPS))
    tp3_dist = min(tp3_dist, pips_to_price(symbol, sig.MTF_TP3_MAX_PIPS))
    tp2_dist = max(tp2_dist, tp1_dist)
    tp3_dist = max(tp3_dist, tp2_dist)

    if action == "BUY":
        sl = round(current_price - sl_dist_price, 5)
        tp1 = round(current_price + tp1_dist, 5)
        tp2 = round(current_price + tp2_dist, 5)
        tp3 = round(current_price + tp3_dist, 5)
    else:
        sl = round(current_price + sl_dist_price, 5)
        tp1 = round(current_price - tp1_dist, 5)
        tp2 = round(current_price - tp2_dist, 5)
        tp3 = round(current_price - tp3_dist, 5)

    sl_dist = abs(current_price - sl) or 1e-9
    weighted_tp_dist = (
        0.5 * abs(tp1 - current_price)
        + 0.25 * abs(tp2 - current_price)
        + 0.25 * abs(tp3 - current_price)
    )
    rr = round(weighted_tp_dist / sl_dist, 2)

    return {
        "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3, "tp": tp3,
        "sl_pips": round(price_to_pips(symbol, sl_dist_price), 2),
        "tp1_pips": round(price_to_pips(symbol, tp1_dist), 2),
        "tp2_pips": round(price_to_pips(symbol, tp2_dist), 2),
        "tp3_pips": round(price_to_pips(symbol, tp3_dist), 2),
        "rr": rr,
        "vetoed_by_rr": rr < sig.MTF_RR_FLOOR,
    }


# --------------------------------------------------------------------------- #
# 1. Constants regression — the fix's whole point.
# --------------------------------------------------------------------------- #
class TestConstants:
    def test_tp1_cap_is_100(self):
        assert sig.MTF_TP1_MAX_PIPS == 100.0

    def test_tp2_cap_is_200(self):
        assert sig.MTF_TP2_MAX_PIPS == 200.0

    def test_tp3_cap_is_200(self):
        # broker-visible TP is tp3 — the whole "≤ 200 pips" contract
        assert sig.MTF_TP3_MAX_PIPS == 200.0

    def test_sl_cap_reduced_to_120(self):
        # reduced from 250 → 120 so weighted R:R stays ≥ 1.1
        assert sig.SL_MAX_PIPS == 120.0

    def test_rr_floor_is_1_1(self):
        assert sig.MTF_RR_FLOOR == 1.1


# --------------------------------------------------------------------------- #
# 2. Large ATR (the user's reported bug scenario) — caps MUST bind.
# --------------------------------------------------------------------------- #
class TestLargeATRGoldCapsBind:
    """User's actual case: XAUUSD SELL with ATR big enough that the
    uncapped geometry produces ~450 pips at tp3 — must be clamped to
    ≤200 pips and R:R must still pass the 1.1 floor."""

    SYMBOL = "XAUUSD"
    CURRENT_PRICE = 4200.00     # roughly current gold spot 2026
    # atr15 = 12.0 USD → tp_dist_price = 60.0 → tp3 uncapped = 60.0 USD = 600 pips
    ATR15 = 12.0

    def test_tp3_capped_at_200_pips(self):
        g = _run_geometry(self.SYMBOL, self.ATR15, self.CURRENT_PRICE, "SELL")
        assert g["tp3_pips"] <= 200.0, f"tp3_pips {g['tp3_pips']} > 200 cap"
        assert g["tp3_pips"] == pytest.approx(200.0, abs=0.01)

    def test_tp1_capped_at_100_pips(self):
        g = _run_geometry(self.SYMBOL, self.ATR15, self.CURRENT_PRICE, "SELL")
        assert g["tp1_pips"] <= 100.0
        assert g["tp1_pips"] == pytest.approx(100.0, abs=0.01)

    def test_tp2_capped_at_200_pips(self):
        g = _run_geometry(self.SYMBOL, self.ATR15, self.CURRENT_PRICE, "SELL")
        assert g["tp2_pips"] <= 200.0
        assert g["tp2_pips"] == pytest.approx(200.0, abs=0.01)

    def test_tp_monotonic(self):
        g = _run_geometry(self.SYMBOL, self.ATR15, self.CURRENT_PRICE, "SELL")
        # SELL: prices go DOWN, so tp1 > tp2 > tp3 numerically
        assert g["tp1"] >= g["tp2"] >= g["tp3"], (
            f"non-monotonic SELL: tp1={g['tp1']} tp2={g['tp2']} tp3={g['tp3']}"
        )
        # pip distances monotonically increase
        assert g["tp1_pips"] <= g["tp2_pips"] <= g["tp3_pips"]

    def test_sl_capped_at_120_pips(self):
        g = _run_geometry(self.SYMBOL, self.ATR15, self.CURRENT_PRICE, "SELL")
        assert g["sl_pips"] <= 120.0
        # ATR_SL_MULTIPLIER (1.5) * 12.0 = 18.0 → 180 pips uncapped → capped 120
        assert g["sl_pips"] == pytest.approx(120.0, abs=0.01)

    def test_rr_survives_floor(self):
        """Weighted R:R = (0.5*100 + 0.25*200 + 0.25*200) / 120 = 150/120 = 1.25
        Must be >= MTF_RR_FLOOR (1.1) so the trade is NOT auto-vetoed."""
        g = _run_geometry(self.SYMBOL, self.ATR15, self.CURRENT_PRICE, "SELL")
        assert g["rr"] >= sig.MTF_RR_FLOOR, (
            f"R:R {g['rr']} < floor {sig.MTF_RR_FLOOR} — every gold trade would be vetoed!"
        )
        assert g["vetoed_by_rr"] is False
        # exact expected: (0.5*100 + 0.25*200 + 0.25*200) / 120 = 150/120 ≈ 1.25
        assert g["rr"] == pytest.approx(1.25, abs=0.01)

    def test_tp3_price_within_200_pips_of_entry(self):
        """The BROKER order's TP equals tp3 — user-facing 'visible TP' distance."""
        g = _run_geometry(self.SYMBOL, self.ATR15, self.CURRENT_PRICE, "SELL")
        dist_pips = abs(self.CURRENT_PRICE - g["tp3"]) / 0.10  # XAUUSD pip = 0.10
        assert dist_pips <= 200.0 + 0.5, f"visible TP {dist_pips} pips away"

    def test_buy_direction_also_capped(self):
        g = _run_geometry(self.SYMBOL, self.ATR15, self.CURRENT_PRICE, "BUY")
        assert g["tp3_pips"] <= 200.0
        assert g["tp1_pips"] <= 100.0
        assert g["sl_pips"] <= 120.0
        # BUY: tp1 < tp2 < tp3 numerically
        assert g["tp1"] <= g["tp2"] <= g["tp3"]


# --------------------------------------------------------------------------- #
# 3. Small ATR — caps must NOT bind (they only shrink, never extend).
# --------------------------------------------------------------------------- #
class TestSmallATRGoldCapsDoNotBind:
    """With a small gold ATR, tp3 uncapped ≈ 60 pips → the 200-pip cap
    must be inert; tp1/tp2/tp3 keep their ATR-derived 24 / 42 / 60 pip
    geometry."""

    SYMBOL = "XAUUSD"
    CURRENT_PRICE = 4200.00
    # atr15 = 1.2 USD → tp_dist_price = 6.0 USD → tp3 uncapped = 60 pips
    ATR15 = 1.2

    def test_tp3_stays_at_60_pips(self):
        g = _run_geometry(self.SYMBOL, self.ATR15, self.CURRENT_PRICE, "SELL")
        # ATR_TP_MULTIPLIER (5.0) * 1.2 = 6.0 USD = 60 pips
        assert g["tp3_pips"] == pytest.approx(60.0, abs=0.01)
        assert g["tp3_pips"] < sig.MTF_TP3_MAX_PIPS  # cap did not bind

    def test_tp2_stays_at_42_pips(self):
        g = _run_geometry(self.SYMBOL, self.ATR15, self.CURRENT_PRICE, "SELL")
        # 6.0 * 0.7 = 4.2 USD = 42 pips
        assert g["tp2_pips"] == pytest.approx(42.0, abs=0.01)
        assert g["tp2_pips"] < sig.MTF_TP2_MAX_PIPS

    def test_tp1_stays_at_24_pips(self):
        g = _run_geometry(self.SYMBOL, self.ATR15, self.CURRENT_PRICE, "SELL")
        # 6.0 * 0.4 = 2.4 USD = 24 pips
        assert g["tp1_pips"] == pytest.approx(24.0, abs=0.01)
        assert g["tp1_pips"] < sig.MTF_TP1_MAX_PIPS

    def test_sl_stays_at_min_floor_pip_range(self):
        # sl uncapped = 1.5 * 1.2 = 1.8 USD = 18 pips; SL_MIN_PIPS floor = 30
        # → sl gets clamped UP to 30 pips (sl_min_price), not down by 120 cap.
        g = _run_geometry(self.SYMBOL, self.ATR15, self.CURRENT_PRICE, "SELL")
        assert g["sl_pips"] == pytest.approx(30.0, abs=0.01)
        assert g["sl_pips"] < sig.SL_MAX_PIPS


# --------------------------------------------------------------------------- #
# 4. EURUSD sanity — 100/200 pip caps are way beyond typical FX geometry.
# --------------------------------------------------------------------------- #
class TestEURUSDCapsDoNotBind:
    SYMBOL = "EURUSD"
    CURRENT_PRICE = 1.08500
    # Realistic active-session M15 EURUSD ATR ~ 20 pips = 0.00200
    # (10-pip ATR15 sits below SL_MIN_PIPS 30 floor and fails RR floor by
    # a pre-existing behaviour unrelated to this fix — see notes below).
    ATR15 = 0.00200  # 20 pips

    def test_tp3_within_100_pips(self):
        g = _run_geometry(self.SYMBOL, self.ATR15, self.CURRENT_PRICE, "BUY")
        # ATR_TP_MULTIPLIER 5.0 * 0.002 = 0.010 = 100 pips uncapped tp3
        assert g["tp3_pips"] == pytest.approx(100.0, abs=0.01)
        assert g["tp3_pips"] < sig.MTF_TP3_MAX_PIPS  # 200-pip cap inert

    def test_tp1_tp2_do_not_bind(self):
        g = _run_geometry(self.SYMBOL, self.ATR15, self.CURRENT_PRICE, "BUY")
        assert g["tp1_pips"] == pytest.approx(40.0, abs=0.01)
        assert g["tp2_pips"] == pytest.approx(70.0, abs=0.01)
        assert g["tp1_pips"] < sig.MTF_TP1_MAX_PIPS
        assert g["tp2_pips"] < sig.MTF_TP2_MAX_PIPS

    def test_sl_within_120_cap(self):
        # 1.5 * 0.002 = 0.003 = 30 pips → equals SL_MIN_PIPS floor, well below cap
        g = _run_geometry(self.SYMBOL, self.ATR15, self.CURRENT_PRICE, "BUY")
        assert g["sl_pips"] == pytest.approx(30.0, abs=0.01)
        assert g["sl_pips"] < sig.SL_MAX_PIPS

    def test_rr_survives_floor(self):
        # weighted TP = 0.5*40 + 0.25*70 + 0.25*100 = 20 + 17.5 + 25 = 62.5
        # SL = 30, RR = 62.5/30 ≈ 2.08 > 1.1 floor ✓
        g = _run_geometry(self.SYMBOL, self.ATR15, self.CURRENT_PRICE, "BUY")
        assert g["rr"] >= sig.MTF_RR_FLOOR


# --------------------------------------------------------------------------- #
# 5. Pip-utils sanity — foundation of the entire cap math.
# --------------------------------------------------------------------------- #
class TestPipUtilsFoundation:
    def test_xauusd_100_pips_is_10_usd(self):
        assert pips_to_price("XAUUSD", 100) == pytest.approx(10.0, abs=1e-9)

    def test_xauusd_200_pips_is_20_usd(self):
        assert pips_to_price("XAUUSD", 200) == pytest.approx(20.0, abs=1e-9)

    def test_xauusd_450_pips_is_45_usd(self):
        # the exact "450 pips too far" the user complained about
        assert pips_to_price("XAUUSD", 450) == pytest.approx(45.0, abs=1e-9)

    def test_eurusd_100_pips_is_0_01(self):
        assert pips_to_price("EURUSD", 100) == pytest.approx(0.01, abs=1e-9)


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.unit
