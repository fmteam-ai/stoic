"""iter-105 · Liquidity Mapping Agent tests."""
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, _BACKEND_DIR)

from liquidity_map import (  # noqa: E402
    build_liquidity_map, liquidity_gate, detect_order_blocks,
    detect_stop_clusters, volume_profile, cumulative_delta, analyze_dom, _atr)


def mk(o, h, l, c, v=100, t=0):
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v}


def flat_bars(n=40, p=100.0):
    return [mk(p, p + 0.5, p - 0.5, p, t=i * 900) for i in range(n)]


def test_not_ready_on_few_bars():
    r = build_liquidity_map([mk(1, 2, 0, 1)] * 10)
    assert r["ready"] is False


def test_demand_order_block_detected():
    bars = flat_bars(30)
    bars.append(mk(100.0, 100.2, 99.0, 99.2))          # bearish OB candle
    bars.append(mk(99.2, 103.5, 99.1, 103.2, v=500))   # bullish displacement
    bars += [mk(103.0, 103.5, 102.5, 103.1) for _ in range(5)]
    obs = detect_order_blocks(bars, _atr(bars))
    assert any(o["dir"] == "DEMAND" and o["bottom"] == 99.0 for o in obs)


def test_supply_order_block_mitigated_is_dropped():
    bars = flat_bars(30)
    bars.append(mk(100.0, 101.0, 99.9, 100.9))          # bullish OB candle
    bars.append(mk(100.9, 101.0, 96.5, 96.8, v=500))    # bearish displacement
    bars.append(mk(96.8, 102.5, 96.7, 102.0))           # closes back above top
    obs = detect_order_blocks(bars, _atr(bars))
    assert not any(o["dir"] == "SUPPLY" for o in obs)


def test_equal_highs_form_buy_side_cluster():
    bars = flat_bars(10)
    for _ in range(3):
        bars.append(mk(100.0, 105.0, 99.8, 100.2))      # swing high @105
        bars += [mk(100.2, 100.8, 99.5, 100.0) for _ in range(4)]
    bars += [mk(100.0, 100.5, 99.5, 100.0) for _ in range(3)]
    cl = detect_stop_clusters(bars, 100.0, _atr(bars))
    buy_side = [c for c in cl if c["side"] == "BUY_SIDE"]
    assert buy_side and buy_side[0]["level"] == 105.0
    assert buy_side[0]["strength"] >= 2


def test_swept_cluster_removed():
    bars = flat_bars(10)
    bars.append(mk(100.0, 105.0, 99.8, 100.2))
    bars += [mk(100.2, 100.8, 99.5, 100.0) for _ in range(5)]
    bars.append(mk(100.0, 106.5, 99.9, 100.3))          # wick sweeps 105
    bars += [mk(100.0, 100.5, 99.5, 100.0) for _ in range(4)]
    cl = detect_stop_clusters(bars, 100.0, _atr(bars))
    assert not any(c["side"] == "BUY_SIDE" and c["level"] == 105.0 for c in cl)


def test_volume_profile_poc():
    bars = [mk(100, 101, 99, 100, v=1000) for _ in range(20)]
    bars += [mk(110, 111, 109, 110, v=10) for _ in range(5)]
    vp = volume_profile(bars)
    assert vp["val"] <= vp["poc"] <= vp["vah"]
    assert vp["poc"] < 105


def test_cumulative_delta_bullish():
    bars = [mk(100 + i * 0.1, 100 + i * 0.1 + 0.5, 100 + i * 0.1 - 0.05,
               100 + i * 0.1 + 0.45, v=200) for i in range(40)]
    d = cumulative_delta(bars)
    assert d["bias"] == "BULLISH"


def test_dom_live_and_imbalance():
    now = datetime.now(timezone.utc).isoformat()
    doc = {"updated_at": now,
           "bids": [{"p": 99.9, "v": 80}, {"p": 99.8, "v": 20}],
           "asks": [{"p": 100.1, "v": 10}, {"p": 100.2, "v": 10}]}
    d = analyze_dom(doc)
    assert d["live"] is True and d["imbalance"] > 0.5
    assert d["wall_bid"]["p"] == 99.9


def test_dom_stale():
    old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    d = analyze_dom({"updated_at": old, "bids": [{"p": 1, "v": 1}], "asks": []})
    assert d["live"] is False


def test_gate_vetoes_buy_into_supply_block():
    bars = flat_bars(30, 100.0)
    bars.append(mk(100.0, 101.0, 99.9, 100.9))          # bullish OB candle
    bars.append(mk(100.9, 101.0, 96.5, 96.8, v=500))    # bearish displacement
    bars += [mk(97.0, 100.5, 96.8, 100.3) for _ in range(3)]  # back inside zone
    lmap = build_liquidity_map(bars, price=100.3)
    assert lmap["active_zone"] == "SUPPLY"
    msg = liquidity_gate("BUY", lmap)
    assert msg and "SUPPLY" in msg
    assert liquidity_gate("SELL", lmap) is None


def test_gate_vetoes_buy_into_ask_wall():
    bars = flat_bars(40)
    now = datetime.now(timezone.utc).isoformat()
    dom = {"updated_at": now, "bids": [{"p": 99.9, "v": 5}],
           "asks": [{"p": 100.1, "v": 95}]}
    lmap = build_liquidity_map(bars, dom_doc=dom)
    msg = liquidity_gate("BUY", lmap)
    assert msg and "ask-heavy" in msg
    assert liquidity_gate("SELL", lmap) is None


def test_gate_fails_open():
    assert liquidity_gate("BUY", None) is None
    assert liquidity_gate("BUY", {"ready": False}) is None
    assert liquidity_gate("HOLD", {"ready": True}) is None


def test_consensus_includes_liquidity_vote():
    from consensus import compute_consensus, WEIGHTS
    assert "liquidity" in WEIGHTS and abs(sum(WEIGHTS.values()) - 1.0) < 1e-9
    sig = {"action": "BUY",
           "liquidity": {"ready": True, "draw": "UP", "active_zone": "DEMAND",
                         "cum_delta": {"bias": "BULLISH"},
                         "dom": {"live": True, "imbalance": 0.4}},
           "confidence": 70}
    r = compute_consensus(sig)
    assert r["votes"]["liquidity"] == 1.0
    sig["action"] = "SELL"
    r2 = compute_consensus(sig)
    assert r2["votes"]["liquidity"] == -1.0


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
