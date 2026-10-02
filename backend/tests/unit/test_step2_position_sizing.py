"""Roadmap step 2 — position sizing correctness (H3 JPY pip size, H4 lot round-up, H5 FX notional)."""
import pytest

from pip_utils import (pip_size, price_to_pips, pip_value_usd_per_lot, pip_value_usd_per_lot_strict,
                       base_symbol, is_jpy_pair, floor_to_lot_step, JPY_PIP, JPY_PIP_VALUE_USD)
from risk import compute_lot_for_account, compute_position_size, get_profile
from risk_engine import _notional, leverage_check
from instruments import notional_usd

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------- H3: JPY pip size
@pytest.mark.parametrize("sym", ["CADJPY", "CHFJPY", "NZDJPY", "CADJPY.fx", "chfjpy-ecn", "NZDJPY#", "EURJPY", "USDJPY.r"])
def test_h3_every_jpy_pair_uses_two_decimal_pip(sym):
    assert is_jpy_pair(sym)
    assert pip_size(sym) == JPY_PIP
    assert price_to_pips(sym, 0.50) == pytest.approx(50.0)        # was 5000 "pips" → lots 100× too small


def test_h3_suffixed_jpy_cross_resolves_to_base():
    assert base_symbol("CADJPY.fx") == "CADJPY"
    assert base_symbol("NZDJPY-ECN") == "NZDJPY"


def test_h3_jpy_pip_value_applies_to_all_jpy_pairs():
    for sym in ("CADJPY", "CHFJPY", "NZDJPY.fx"):
        assert pip_value_usd_per_lot(sym) == JPY_PIP_VALUE_USD
        assert pip_value_usd_per_lot_strict(sym) == JPY_PIP_VALUE_USD
    assert pip_value_usd_per_lot("CADJPY", "cent") == pytest.approx(JPY_PIP_VALUE_USD * 0.01)


def test_h3_non_jpy_unchanged():
    assert pip_size("EURUSD") == 0.0001 and pip_size("XAUUSD.fx") == 0.10
    assert not is_jpy_pair("EURUSD") and not is_jpy_pair("XAUUSD")
    assert pip_value_usd_per_lot_strict("EURGBP") is None               # cross stays UNKNOWN (fail-closed)


def test_h3_jpy_lot_sizing_matches_budget():
    acct = {"equity": 10_000.0, "account_type": "standard"}
    prof = get_profile("medium")
    # 50-pip stop on CADJPY (0.50 price distance)
    out = compute_lot_for_account(acct, "CADJPY.fx", 108.00, 107.50, 75, prof)
    assert out["sizing_valid"]
    assert out["sl_pips"] == pytest.approx(50.0)
    budget = 10_000 * prof["risk_pct"] / 100
    assert out["actual_risk_usd"] <= budget + 1e-6


# ---------------------------------------------------------------- H4: floor, never round up
@pytest.mark.parametrize("lots,expected", [(0.015, 0.01), (0.019999, 0.01), (0.0449, 0.04), (0.125, 0.12),
                                           (1.0, 1.0), (0.03, 0.03), (0.009, 0.0), (0.0, 0.0)])
def test_h4_floor_to_lot_step(lots, expected):
    assert floor_to_lot_step(lots) == pytest.approx(expected)


def test_h4_floor_never_exceeds_risk_budget():
    acct = {"equity": 1_000.0, "account_type": "standard"}
    prof = get_profile("medium")
    # pick a stop so raw lots land at x.xx5 — old round() went UP, floor goes DOWN
    budget = 1_000 * prof["risk_pct"] / 100
    pip_usd = pip_value_usd_per_lot("XAUUSD")
    sl_pips = budget / (0.0150 * pip_usd)                      # raw lots = 0.0150
    out = compute_lot_for_account(acct, "XAUUSD", 4000.0, 4000.0 - sl_pips * 0.10, 75, prof)
    assert out["sizing_valid"]
    assert out["lot_size"] == 0.01                             # not 0.02 (+33% risk)
    assert out["actual_risk_usd"] <= out["risk_amount_usd"] + 1e-6


def test_h4_legacy_sizer_floors_too():
    # equity 1000, 1% → $10 budget, 25 pips × $10 → 0.04 exactly; 0.045 case below
    assert compute_position_size(1000, 1.0, 25, 10.0) == pytest.approx(0.04)
    assert compute_position_size(1000, 1.125, 25, 10.0) == pytest.approx(0.04)   # 0.045 → 0.04 (was 0.05)


# ---------------------------------------------------------------- H5: FX notional
def test_h5_fx_notional_uses_contract_units():
    assert _notional("EURUSD", 1.0, 1.10) == pytest.approx(110_000.0)
    assert _notional("EURUSD.fx", 0.10, 1.10) == pytest.approx(11_000.0)
    assert _notional("USDJPY", 0.5, 150.0) == pytest.approx(50_000.0)     # base IS USD
    assert _notional("XAUUSD", 1.0, 4000.0) == 400_000.0                 # unchanged
    assert _notional("US30", 1.0, 44000.0) == 44_000.0
    assert _notional("EURUSD", 1.0, 1.10) == notional_usd("EURUSD", 1.0, 1.10)


def test_h5_leverage_check_now_sees_fx_exposure():
    bars = []
    # $1,000 equity · 20× cap = $20,000 — 1.0 lot EURUSD is $110,000 → must trim
    r = leverage_check(1000, "EURUSD", 1.0, 1.10, bars, None, max_leverage=20)
    assert r["status"] == "trim" and r["scale"] < 0.2
    ok = leverage_check(1000, "EURUSD", 0.1, 1.10, bars, None, max_leverage=20)
    assert ok["status"] == "ok"
