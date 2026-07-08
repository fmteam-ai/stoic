"""iter-123 · Range Scalp engine — unit tests for the deterministic setup."""
from intraday_features import range_scalp_signal


def _feats(**over):
    base = {
        "trend": "FLAT", "donchian20": "INSIDE", "atr15": 3.0,
        "session_high": 4090.0, "session_low": 4050.0,
        "range_pos_pct": 50.0, "day_range_pct": 1.0,
        "session_vwap": 4070.0, "last_price": 4070.0,
    }
    base.update(over)
    return base


def test_buy_at_range_low():
    a, note = range_scalp_signal(_feats(range_pos_pct=10.0, last_price=4054.0))
    assert a == "BUY" and "fading the low" in note


def test_sell_at_range_high():
    a, note = range_scalp_signal(_feats(range_pos_pct=90.0, last_price=4086.0))
    assert a == "SELL" and "fading the high" in note


def test_mid_range_holds():
    a, _ = range_scalp_signal(_feats(range_pos_pct=50.0))
    assert a is None


def test_trending_market_stands_down():
    assert range_scalp_signal(_feats(trend="DOWN", range_pos_pct=90.0))[0] is None
    assert range_scalp_signal(_feats(donchian20="BREAK_UP", range_pos_pct=10.0))[0] is None


def test_narrow_or_quiet_range_rejected():
    # range width 40 vs ATR 15 → 40 < 4×15, target unreachable
    assert range_scalp_signal(_feats(atr15=15.0, range_pos_pct=10.0))[0] is None
    assert range_scalp_signal(_feats(day_range_pct=0.3, range_pos_pct=10.0))[0] is None


def test_missing_pack():
    assert range_scalp_signal(None) == (None, "")
