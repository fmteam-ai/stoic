"""Roadmap step 6 — signal data correctness (H8 gaps, H9 look-ahead, H10 forming candle)."""
import time
import pytest

from kalman import kalman_smooth
from routes.bridge_routes import split_forming_bar, timeframe_seconds

pytestmark = pytest.mark.unit


# ------------------------------------------------------------ H8/H9 Kalman alignment
def test_kalman_output_is_index_aligned_with_gaps():
    prices = [100, 101, None, 103, None, None, 106]
    out = kalman_smooth(prices)
    assert len(out) == len(prices)
    assert [bool(o.get("imputed")) for o in out] == [False, False, True, False, True, True, False]
    assert out[3]["price"] == 103 and out[6]["price"] == 106            # measurements stay on their bar


def test_kalman_no_lookahead_through_a_gap():
    """The value at bar i must not change when FUTURE bars change (causal filter)."""
    a = [100, 101, None, 103, 104, 105, 106]
    b = [100, 101, None, 103, 140, 90, 200]                              # different future
    oa, ob = kalman_smooth(a), kalman_smooth(b)
    for i in range(4):
        assert oa[i]["k_price"] == pytest.approx(ob[i]["k_price"])
        assert oa[i]["k_velocity"] == pytest.approx(ob[i]["k_velocity"])


def test_kalman_old_behaviour_would_have_shifted():
    """Regression: before the fix, dropping the None shifted bar 3's state onto index 2."""
    prices = [100, 101, None, 103, 104]
    out = kalman_smooth(prices)
    dense = kalman_smooth([100, 101, 103, 104])
    # index 3 (103) in the gapped series must NOT equal the dense series' index 2 state verbatim
    # for velocity — the gap is a predict-only step, not a deleted bar.
    assert out[3]["price"] == 103 and dense[2]["price"] == 103
    assert out[4]["k_velocity"] != pytest.approx(dense[3]["k_velocity"]) or len(out) != len(dense)


def test_kalman_leading_gaps_and_degenerate_inputs():
    assert kalman_smooth([None, None]) == []
    assert kalman_smooth([]) == []
    out = kalman_smooth([None, 50, 51])
    assert len(out) == 3 and out[0]["imputed"] and out[1]["price"] == 50
    assert kalman_smooth([7])[0]["k_velocity"] == 0.0


# ------------------------------------------------------------ H10 forming candle
def test_timeframe_seconds():
    assert timeframe_seconds("M15") == 900 and timeframe_seconds("h1") == 3600
    assert timeframe_seconds("weird") == 900


def test_forming_bar_is_dropped_closed_bars_kept():
    now = 1_700_000_000
    bars = [{"t": now - 2700}, {"t": now - 1800}, {"t": now - 900}, {"t": now - 300}]   # last opened 5 min ago
    closed, dropped = split_forming_bar(bars, "M15", now)
    assert dropped == 1 and [b["t"] for b in closed] == [now - 2700, now - 1800, now - 900]


def test_bar_closing_exactly_now_counts_as_closed():
    now = 1_700_000_000
    closed, dropped = split_forming_bar([{"t": now - 900}], "M15", now)
    assert dropped == 0 and len(closed) == 1


def test_only_forming_bar_sent_yields_empty_not_error():
    now = int(time.time())
    closed, dropped = split_forming_bar([{"t": now - 10}], "M1", now)
    assert closed == [] and dropped == 1


def test_receive_candles_uses_split():
    import inspect
    from routes import bridge_routes
    src = inspect.getsource(bridge_routes.receive_candles)
    assert "split_forming_bar(" in src and "forming_dropped" in src
