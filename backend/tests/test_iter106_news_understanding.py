"""iter-106 · AI News Understanding Agent tests (pure functions, no network)."""
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, _BACKEND_DIR)

from news_understanding import (  # noqa: E402
    aggregate_scores, news_gate, _parse_array, _recency_weight, _label)

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)


def _it(score, hours_ago=1, title="headline", why="w"):
    return {"score": score, "title": title, "source": "Reuters", "why": why,
            "publishedAt": (NOW - timedelta(hours=hours_ago)).isoformat()}


def test_aggregate_simple_mean():
    snap = aggregate_scores([_it(3), _it(1)], now=NOW)
    assert 1.9 < snap["net"] < 2.1
    assert snap["label"] == "strongly_bullish"
    assert snap["headlines"] == 2


def test_recency_weighting_favours_fresh():
    snap = aggregate_scores([_it(3, hours_ago=1), _it(-3, hours_ago=48)], now=NOW)
    assert snap["net"] > 1.5  # fresh +3 dominates stale -3


def test_scores_clamped_to_plus_minus_3():
    snap = aggregate_scores([_it(9), _it(-9)], now=NOW)
    assert -3 <= snap["net"] <= 3
    for d in snap["drivers"]:
        assert -3 <= d["score"] <= 3


def test_drivers_sorted_by_magnitude():
    snap = aggregate_scores([_it(0.5, title="a"), _it(-3, title="b"),
                             _it(2, title="c"), _it(1, title="d")], now=NOW)
    assert [d["title"] for d in snap["drivers"]] == ["b", "c", "d"]


def test_labels():
    assert _label(2.5) == "strongly_bullish"
    assert _label(1.0) == "bullish"
    assert _label(0.0) == "neutral"
    assert _label(-1.0) == "bearish"
    assert _label(-2.5) == "strongly_bearish"


def test_aggregate_empty_returns_none():
    assert aggregate_scores([]) is None
    assert aggregate_scores([{"title": "x"}]) is None


def test_recency_half_life():
    fresh = _recency_weight(NOW.isoformat(), NOW)
    half = _recency_weight((NOW - timedelta(hours=12)).isoformat(), NOW)
    assert abs(fresh - 1.0) < 0.01 and abs(half - 0.5) < 0.01
    assert _recency_weight("garbage", NOW) < 0.3  # unparseable = stale


def test_parse_array_handles_fences_and_prose():
    raw = 'Here you go:\n```json\n[{"i":0,"score":-2.5,"why":"hot CPI"}]\n```'
    arr = _parse_array(raw)
    assert arr[0]["score"] == -2.5
    assert _parse_array("not json") == []


def test_gate_vetoes_buy_on_strongly_bearish():
    snap = {"net": -2.3, "headlines": 10,
            "drivers": [{"title": "CPI comes in hot, yields surge"}]}
    msg = news_gate("BUY", "XAUUSD", snap)
    assert msg and "strongly bearish" in msg and "CPI" in msg
    assert news_gate("SELL", "XAUUSD", snap) is None


def test_gate_vetoes_sell_on_strongly_bullish():
    snap = {"net": 2.1, "headlines": 8, "drivers": [{"title": "Fed signals cuts"}]}
    assert news_gate("SELL", "XAUUSD", snap)
    assert news_gate("BUY", "XAUUSD", snap) is None


def test_gate_fails_open():
    assert news_gate("BUY", "XAUUSD", None) is None
    assert news_gate("BUY", "XAUUSD", {"net": -1.5}) is None  # below threshold
    assert news_gate("HOLD", "XAUUSD", {"net": -3}) is None


def test_consensus_macro_includes_news():
    from consensus import compute_consensus
    base = {"action": "BUY", "confidence": 60}
    with_news = compute_consensus({**base, "news_ai": {"net": 2.0}})
    without = compute_consensus(base)
    assert with_news["votes"]["macro"] > without["votes"]["macro"]
    against = compute_consensus({**base, "news_ai": {"net": -2.0}})
    assert against["votes"]["macro"] < without["votes"]["macro"]


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.unit
