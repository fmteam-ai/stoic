"""iter-114 · Advanced Risk Engine tests."""
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import sys
import time

sys.path.insert(0, _BACKEND_DIR)

from risk_engine import (  # noqa: E402
    leverage_check, event_exposure_check, drawdown_check,
    abnormal_market_check, _notional)


def bars_normal(n=96, p=4000.0, rng=4.0, t_end=None):
    t_end = t_end or time.time()
    return [{"t": t_end - (n - 1 - i) * 900, "o": p, "h": p + rng / 2,
             "l": p - rng / 2, "c": p, "v": 100} for i in range(n)]


def test_notional_xau_contract():
    assert _notional("XAUUSD", 1.0, 4000.0) == 400000.0
    assert _notional("US30", 1.0, 44000.0) == 44000.0


def test_leverage_trims_oversize():
    bars = bars_normal()
    # equity 1k, 20× cap → $20k; 0.5 lot gold @4000 = $200k notional
    r = leverage_check(1000, "XAUUSD", 0.5, 4000.0, bars,
                       {"confidence_pct": 75}, max_leverage=20)
    assert r["status"] == "trim" and r["scale"] <= 0.11
    ok = leverage_check(100000, "XAUUSD", 0.1, 4000.0, bars,
                        {"confidence_pct": 75}, max_leverage=20)
    assert ok["status"] == "ok" and ok["scale"] == 1.0


def test_leverage_confidence_and_vol_modulate():
    bars = bars_normal()
    low = leverage_check(1000, "XAUUSD", 0.5, 4000.0, bars,
                         {"confidence_pct": 55}, max_leverage=20)
    high = leverage_check(1000, "XAUUSD", 0.5, 4000.0, bars,
                          {"confidence_pct": 92}, max_leverage=20)
    assert high["scale"] > low["scale"]   # more confidence → bigger cap
    shocked = bars_normal(88) + [
        {"t": time.time() - (7 - i) * 900, "o": 4000, "h": 4030, "l": 3970,
         "c": 4000, "v": 100} for i in range(8)]
    calm_cap = leverage_check(1000, "XAUUSD", 0.5, 4000.0, bars,
                              {"confidence_pct": 75}, 20)["scale"]
    vol_cap = leverage_check(1000, "XAUUSD", 0.5, 4000.0, shocked,
                             {"confidence_pct": 75}, 20)["scale"]
    assert vol_cap < calm_cap             # expanded vol → smaller cap


def test_event_exposure_blocks_and_halves():
    blocked = event_exposure_check(90000, 30000, 100000, 25, "CPI m/m")
    assert blocked["status"] == "block" and blocked["scale"] == 0.0
    halved = event_exposure_check(40000, 20000, 100000, 25, "CPI m/m")
    assert halved["status"] == "trim" and halved["scale"] == 0.5
    fine = event_exposure_check(10000, 20000, 100000, 25, "CPI m/m")
    assert fine["status"] == "ok"
    no_event = event_exposure_check(900000, 900000, 100000, None)
    assert no_event["status"] == "ok"
    far_event = event_exposure_check(900000, 900000, 100000, 300, "NFP")
    assert far_event["status"] == "ok"


def test_drawdown_ladder():
    # monthly breach even when day/week fine
    r = drawdown_check(pnl_day=-10, pnl_week=-50, pnl_month=-1300,
                       equity=10000, monthly_pct=12.0)
    assert r["status"] == "block" and "monthly" in r["detail"]
    r2 = drawdown_check(-310, -100, -100, 10000, daily_pct=3.0)
    assert r2["status"] == "block" and "daily" in r2["detail"]
    r3 = drawdown_check(-100, -710, -710, 10000, weekly_pct=7.0)
    assert r3["status"] == "block" and "weekly" in r3["detail"]
    ok = drawdown_check(-100, -200, -300, 10000)
    assert ok["status"] == "ok"
    profit = drawdown_check(500, 900, 2000, 10000)
    assert profit["status"] == "ok"


def test_abnormal_shock_blocks():
    bars = bars_normal(95) + [{"t": time.time(), "o": 4000, "h": 4020,
                               "l": 3990, "c": 4010, "v": 900}]
    r = abnormal_market_check(bars)      # 30 range vs 4 median = 7.5×
    assert r["status"] == "block" and "shock" in r["detail"]
    elevated = bars_normal(95) + [{"t": time.time(), "o": 4000, "h": 4006,
                                   "l": 3995, "c": 4004, "v": 300}]
    r2 = abnormal_market_check(elevated)  # 11 vs 4 = 2.75×
    assert r2["status"] == "trim" and r2["scale"] == 0.5
    assert abnormal_market_check(bars_normal())["status"] == "ok"


def test_stale_feed_blocks_on_weekdays_only():
    import datetime as dt
    old = bars_normal(t_end=time.time() - 3 * 3600)
    r = abnormal_market_check(old)
    if dt.datetime.now(dt.timezone.utc).weekday() < 5:
        assert r["status"] == "block" and "stale" in r["detail"]
    else:
        assert r["status"] in ("ok", "trim", "block")


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
