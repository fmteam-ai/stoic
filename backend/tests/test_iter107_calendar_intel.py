"""iter-107 · Economic Calendar Intelligence tests."""
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import sys
import time

sys.path.insert(0, _BACKEND_DIR)

from calendar_intel import (  # noqa: E402
    classify_event, context_multipliers, calendar_entry_policy,
    classify_outcome, OUTCOMES, PRIORS)


def mk(o, h, l, c, t, v=100):
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v}


def flat(n, p=100.0, t0=0, amp=0.5):
    return [mk(p, p + amp, p - amp, p, t0 + i * 900) for i in range(n)]


def test_classify_event():
    assert classify_event("FOMC Statement") == "FOMC"
    assert classify_event("Federal Funds Rate") == "FOMC"
    assert classify_event("CPI m/m") == "CPI"
    assert classify_event("Core PCE Price Index") == "CPI"
    assert classify_event("Non-Farm Employment Change") == "NFP"
    assert classify_event("Unemployment Rate") == "NFP"
    assert classify_event("Advance GDP q/q") == "GDP"
    assert classify_event("ISM Manufacturing PMI") == "PMI"
    assert classify_event("Fed Chair Powell Speaks") == "SPEECH"
    assert classify_event("Building Permits") == "OTHER"


def test_priors_cover_all_outcomes():
    for cls, p in PRIORS.items():
        assert set(p) == set(OUTCOMES), cls


def test_compression_boosts_breakout():
    bars = flat(80, amp=2.0) + flat(16, t0=80 * 900, amp=0.3)
    mult, drivers = context_multipliers(bars)
    assert mult["breakout"] > 1.0
    assert any("compressed" in d for d in drivers)


def test_stop_clusters_boost_fakeout():
    bars = flat(40)
    lmap = {"ready": True, "atr": 1.0,
            "stop_clusters": [{"side": "BUY_SIDE", "level": 101, "strength": 3,
                               "dist": 1.0}]}
    mult, drivers = context_multipliers(bars, lmap)
    assert mult["fakeout"] > 1.0
    assert any("stop-hunt" in d for d in drivers)


def test_policy_vetoes_fakeout_dominant_pre_event():
    pred = {"title": "CPI m/m", "minutes_to": 20, "top": "fakeout",
            "probs": {"breakout": 0.2, "fakeout": 0.45, "reversal": 0.15,
                      "continuation": 0.2}}
    pol = calendar_entry_policy("BUY", pred)
    assert pol["mode"] == "VETO" and "stop-hunt" in pol["reason"]


def test_policy_caution_when_continuation_dominant():
    pred = {"title": "GDP", "minutes_to": 20, "top": "continuation",
            "probs": {"breakout": 0.2, "fakeout": 0.2, "reversal": 0.15,
                      "continuation": 0.45}}
    pol = calendar_entry_policy("BUY", pred)
    assert pol["mode"] == "CAUTION"


def test_policy_post_event_trap_veto():
    pred = {"title": "NFP", "minutes_to": -5, "top": "fakeout",
            "probs": {"fakeout": 0.4, "breakout": 0.3, "reversal": 0.1,
                      "continuation": 0.2}}
    pol = calendar_entry_policy("SELL", pred)
    assert pol["mode"] == "VETO" and "trap" in pol["reason"]


def test_policy_none_outside_window():
    pred = {"title": "NFP", "minutes_to": 300, "top": "fakeout",
            "probs": {"fakeout": 0.5}}
    assert calendar_entry_policy("BUY", pred) is None
    assert calendar_entry_policy("HOLD", pred) is None
    assert calendar_entry_policy("BUY", None) is None


def _event_bars(post):
    """8 flat pre bars @100 (t=0..7) then event at t=8 bars follow `post`."""
    bars = flat(8, 100.0)
    t0 = 8 * 900
    for i, (h, l, c) in enumerate(post):
        bars.append(mk(bars[-1]["c"], h, l, c, t0 + i * 900))
    return bars, float(t0)


def test_outcome_breakout():
    bars, ts = _event_bars([(103, 100, 102.5), (103.5, 102, 103),
                            (104, 102.5, 103.5), (104, 103, 103.8)])
    assert classify_outcome(bars, ts) == "breakout"


def test_outcome_fakeout():
    bars, ts = _event_bars([(103, 99.8, 100.2), (100.6, 99.6, 100.1),
                            (100.5, 99.5, 100.0), (100.4, 99.6, 100.1)])
    assert classify_outcome(bars, ts) == "fakeout"


def test_outcome_continuation():
    bars, ts = _event_bars([(100.4, 99.7, 100.1), (100.3, 99.8, 100.0),
                            (100.4, 99.7, 100.2), (100.3, 99.8, 100.1)])
    assert classify_outcome(bars, ts) == "continuation"


def test_outcome_reversal():
    bars = [mk(100 + i * 0.4, 100 + i * 0.4 + 0.3, 100 + i * 0.4 - 0.3,
               100 + (i + 1) * 0.4, i * 900) for i in range(8)]  # uptrend into print
    t0 = 8 * 900
    last = bars[-1]["c"]
    for i in range(4):  # hard sell-off closing below the pre-range low
        c = last - (i + 1) * 1.2
        bars.append(mk(c + 1.2, c + 1.4, c - 0.2, c, t0 + i * 900))
    r = classify_outcome(bars, float(t0))
    assert r == "reversal"


def test_outcome_none_when_insufficient():
    assert classify_outcome([], 0) is None
    bars = flat(5)
    assert classify_outcome(bars, 10 * 900) is None  # event beyond data


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.unit
