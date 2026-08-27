"""iter-125 · MTF Confluence engine — unit tests."""
from mtf_intraday import resample, tf_trend, detect_pullback, analyze_mtf_confluence


def _bars(closes, start=0, spread=1.0):
    return [{"t": start + i * 900, "o": c, "h": c + spread, "l": c - spread, "c": c, "v": 100}
            for i, c in enumerate(closes)]


def test_resample_grouping():
    bars = _bars([100 + i for i in range(32)])
    h1 = resample(bars, 4)
    h4 = resample(bars, 16)
    assert len(h1) == 8 and len(h4) == 2
    assert h1[0]["c"] == 103 and h1[0]["h"] == 104  # last close, max high of group


def test_tf_trend_directions():
    assert tf_trend(_bars([100 + i * 2 for i in range(30)])) == "UP"
    assert tf_trend(_bars([200 - i * 2 for i in range(30)])) == "DOWN"
    assert tf_trend(_bars([100 + (1 if i % 2 else -1) for i in range(30)])) == "FLAT"


def test_pullback_detection_up():
    # impulse up 100→130, then retrace to ~118 (40%)
    closes = [100 + i * 2.5 for i in range(13)] + [130 - i * 1.5 for i in range(9)]
    pb = detect_pullback(_bars(closes), "UP", atr15=3.0)
    assert pb["ready"], pb["note"]
    assert pb["swing_level"] is not None


def test_pullback_rejects_too_deep():
    closes = [100 + i * 2.5 for i in range(13)] + [130 - i * 3.0 for i in range(9)]
    pb = detect_pullback(_bars(closes), "UP", atr15=3.0)
    assert not pb["ready"]


def _trend_pullback_series():
    """Base uptrend, steep impulse, then a ~36% pullback — a textbook setup."""
    return ([4000 + i * 0.5 for i in range(500)]
            + [4250 + i * 3 for i in range(30)]
            + [4340 - i * 5 for i in range(6)])


def test_full_confluence_long():
    bars = _bars(_trend_pullback_series())
    rep = analyze_mtf_confluence(bars, live_price=4400.0, atr15=6.0)
    assert rep["h4_trend"] == "UP" and rep["h1_structure"] == "UP", rep["note"]
    assert rep["m15_setup"]["ready"], rep["m15_setup"]["note"]
    assert rep["entry_trigger"] and rep["direction"] == "BUY" and rep["aligned"]


def test_no_alignment_when_tfs_disagree():
    # 4H up overall but last ~2 days selling off hard → 1H DOWN
    closes = [4000 + i * 0.6 for i in range(440)] + [4264 - i * 1.2 for i in range(200)]
    rep = analyze_mtf_confluence(_bars(closes), live_price=4000.0, atr15=5.0)
    assert not rep["aligned"]


def test_entry_waits_for_breakout():
    rep = analyze_mtf_confluence(_bars(_trend_pullback_series()),
                                 live_price=4200.0, atr15=6.0)  # below swing
    if rep["m15_setup"] and rep["m15_setup"]["ready"]:
        assert not rep["entry_trigger"] and not rep["aligned"]


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.unit
