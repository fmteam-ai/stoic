"""iter-60 · Market Structure agent + Range forecast + Fed tone + Posture."""
import os
import sys

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from market_structure import (  # noqa: E402
    find_swings, detect_bos, detect_sweeps, detect_fvg, acc_dist,
    structure_snapshot, structure_gate,
)
from range_forecast import build_range_forecast, range_gate  # noqa: E402
from fed_tone import fed_tone_gate  # noqa: E402

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://stoic-trading-bot.preview.emergentagent.com").rstrip("/")


def bar(o, h, l, c, t=0, v=100):
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v}


def rally_bars(cycles=8, start=100.0):
    """Zig-zag up: 3-bar impulse legs (+1 each) with 2-bar pullbacks (-0.6),
    so fractal swing highs form and later legs break them (bullish BOS)."""
    bars, p = [], start
    for c in range(cycles):
        for _ in range(3):
            o = p; p += 1.0
            bars.append(bar(o, p + 0.2, o - 0.2, p, t=1000 + len(bars) * 900))
        for _ in range(2):
            o = p; p -= 0.6
            bars.append(bar(o, o + 0.2, p - 0.2, p, t=1000 + len(bars) * 900))
    return bars


def selloff_bars(cycles=8, start=200.0):
    bars, p = [], start
    for c in range(cycles):
        for _ in range(3):
            o = p; p -= 1.0
            bars.append(bar(o, o + 0.2, p - 0.2, p, t=1000 + len(bars) * 900))
        for _ in range(2):
            o = p; p += 0.6
            bars.append(bar(o, p + 0.2, o - 0.2, p, t=1000 + len(bars) * 900))
    return bars


class TestDetectors:
    def test_swings_found(self):
        bars = rally_bars()
        highs, lows = find_swings(bars)
        assert highs and lows

    def test_bullish_bos_in_rally(self):
        bars = rally_bars()
        highs, lows = find_swings(bars)
        events = detect_bos(bars, highs, lows)
        assert events and events[-1]["dir"] == "BULLISH"

    def test_bearish_bos_in_selloff(self):
        bars = selloff_bars()
        highs, lows = find_swings(bars)
        events = detect_bos(bars, highs, lows)
        assert events and events[-1]["dir"] == "BEARISH"

    def test_liquidity_sweep_detected(self):
        # build a swing high at 105 then a bar that wicks to 105.5 but closes at 104
        bars = [bar(100, 101, 99.5, 100.5), bar(100.5, 103, 100, 102),
                bar(102, 105, 101.5, 104), bar(104, 104.5, 102, 103),
                bar(103, 103.5, 101, 102), bar(102, 105.5, 101.5, 104.0),
                bar(104, 104.2, 102.5, 103)]
        highs, lows = find_swings(bars)
        sweeps = detect_sweeps(bars, highs, lows)
        assert any(s["side"] == "BUY_SIDE" for s in sweeps)

    def test_fvg_detected_and_fill_respected(self):
        bars = [bar(100, 101, 99, 100.5), bar(101, 104, 100.8, 103.5),
                bar(103.5, 105, 102.5, 104.5)]  # bar3.low 102.5 > bar1.high 101 → bullish FVG
        gaps = detect_fvg(bars)
        assert gaps and gaps[0]["dir"] == "BULLISH"
        filled = bars + [bar(104, 104.5, 100.5, 101)]  # trades through the gap
        assert detect_fvg(filled) == []

    def test_acc_dist_phases(self):
        # closes near highs → accumulation
        acc = [bar(100 + i * 0.1, 100 + i * 0.1 + 1, 100 + i * 0.1 - 0.1,
                   100 + i * 0.1 + 0.9) for i in range(40)]
        assert acc_dist(acc)["phase"] == "ACCUMULATION"
        dist = [bar(100, 101.1, 100 - 1, 100 - 0.9) for _ in range(40)]
        assert acc_dist(dist)["phase"] == "DISTRIBUTION"


class TestStructureGate:
    def test_sell_vetoed_after_bullish_bos(self):
        snap = structure_snapshot(rally_bars())
        assert snap["ready"] and snap["bias"] == "BULLISH"
        assert structure_gate("SELL", snap) is not None
        assert structure_gate("BUY", snap) is None

    def test_no_veto_when_not_ready(self):
        assert structure_gate("SELL", {"ready": False}) is None
        assert structure_gate("SELL", structure_snapshot([])) is None

    def test_neutral_bias_allows_both(self):
        snap = {"ready": True, "bias": "NEUTRAL", "last_bos": None}
        assert structure_gate("SELL", snap) is None
        assert structure_gate("BUY", snap) is None


DAILY = [{"high": 105 + i * 0.1, "low": 95 + i * 0.1, "close": 100 + i * 0.1,
          "date": f"2026-06-{i+1:02d}"} for i in range(20)]


class TestRangeForecast:
    def test_atr_computed(self):
        f = build_range_forecast(DAILY)
        assert f and f["atr14"] == pytest.approx(10.0, abs=0.5)

    def test_remaining_range_from_intraday(self):
        intraday = [{"t": 86400 * 100 + i * 900, "o": 100, "h": 104, "l": 96, "c": 100, "v": 1}
                    for i in range(10)]
        f = build_range_forecast(DAILY, intraday)
        assert f["used_range"] == pytest.approx(8.0, abs=0.1)
        assert f["remaining_range"] == pytest.approx(f["atr14"] - 8.0, abs=0.2)

    def test_range_gate_blocks_unreachable_tp(self):
        f = {"atr14": 10.0, "remaining_range": 2.0, "used_pct": 80.0}
        assert range_gate("BUY", 100.0, 105.0, f) is not None
        assert range_gate("BUY", 100.0, 101.5, f) is None

    def test_gate_fails_open(self):
        assert range_gate("BUY", 100.0, 105.0, None) is None
        assert range_gate("BUY", 100.0, 105.0, {"atr14": 10.0}) is None


class TestFedToneGate:
    def test_hawkish_blocks_gold_buy(self):
        tone = {"score": 0.8, "label": "hawkish", "summary": "hikes ahead"}
        assert fed_tone_gate("BUY", "XAUUSD", tone) is not None
        assert fed_tone_gate("SELL", "XAUUSD", tone) is None

    def test_dovish_blocks_gold_sell(self):
        tone = {"score": -0.9, "label": "dovish", "summary": "cuts ahead"}
        assert fed_tone_gate("SELL", "XAUUSD", tone) is not None

    def test_moderate_tone_no_gate(self):
        assert fed_tone_gate("BUY", "XAUUSD", {"score": 0.5}) is None

    def test_non_gold_ignored(self):
        assert fed_tone_gate("BUY", "BTCUSD", {"score": 0.9}) is None

    def test_none_tone_fails_open(self):
        assert fed_tone_gate("BUY", "XAUUSD", None) is None


@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": "admin@trading.bot", "password": "admin123"}, timeout=15)
    assert r.status_code == 200
    return s


class TestHttp:
    def test_posture_endpoint(self, session):
        r = session.get(f"{BASE_URL}/api/posture".replace("/posture", "/bot/posture"), timeout=30)
        assert r.status_code == 200
        d = r.json()
        assert "symbols" in d and "risk" in d and "macro" in d

    def test_candles_endpoint_rejects_bad_token(self):
        r = requests.post(f"{BASE_URL}/api/bridge/candles",
                          json={"bridge_token": "bogus", "symbol": "XAUUSD",
                                "bars": [{"t": 1, "o": 1, "h": 1, "l": 1, "c": 1}]},
                          timeout=15)
        assert r.status_code == 401


class TestWiring:
    def test_bot_runner_gates(self):
        src = open(os.path.join(BACKEND, "bot_runner.py")).read()
        for token in ("structure_gate", "range_gate", "fed_tone_gate",
                      '"structure_gate_veto"', '"range_gate_veto"', '"fed_tone_veto"'):
            assert token in src, token

    def test_ea_142_candle_feed(self):
        from ea_version import current_ea_version
        v = current_ea_version()
        src = open(os.path.join(BACKEND, "static", "EmergentTradingBridge.mq5")).read()
        assert f'#property version   "{v}"' in src
        assert f'#define EA_CLIENT_VERSION "{v}"' in src
        assert "void SendCandles()" in src and "/api/bridge/candles" in src
        assert "input int    CandlesSeconds" in src
