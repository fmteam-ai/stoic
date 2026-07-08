"""iter-120 · Intraday scalp engine — unit tests for the M15 feature pack."""
import time

from intraday_features import compute_intraday_features, intraday_alignment, MIN_BARS


def _bars(closes, spread=1.0, start_ts=None):
    ts = start_ts or (int(time.time()) - len(closes) * 900)
    out = []
    for i, c in enumerate(closes):
        out.append({"t": ts + i * 900, "o": c + 0.2, "h": c + spread,
                    "l": c - spread, "c": c, "v": 100})
    return out


def test_too_few_bars_returns_none():
    assert compute_intraday_features(_bars([4000] * (MIN_BARS - 1))) is None


def test_waterfall_scores_strong_sell_alignment():
    # 96 bars: flat then a steady -80pt waterfall (like 2026-07-08 11:00-13:00)
    closes = [4120.0] * 40 + [4120 - i * 1.5 for i in range(56)]
    feats = compute_intraday_features(_bars(closes))
    assert feats["trend"] == "DOWN"
    assert feats["donchian20"] == "BREAK_DOWN"
    assert feats["momentum_3h_pct"] < -0.25
    assert feats["swing_structure"] == "LH_LL"
    score, note = intraday_alignment("SELL", feats)
    assert score >= 60, f"waterfall must clear scalp threshold, got {score} ({note})"
    buy_score, _ = intraday_alignment("BUY", feats)
    assert buy_score < 60


def test_rally_scores_strong_buy_alignment():
    closes = [4000.0] * 40 + [4000 + i * 1.5 for i in range(56)]
    feats = compute_intraday_features(_bars(closes))
    score, _ = intraday_alignment("BUY", feats)
    assert score >= 60
    sell_score, _ = intraday_alignment("SELL", feats)
    assert sell_score < 60


def test_chop_stays_below_threshold():
    closes = [4100 + (3 if i % 2 else -3) for i in range(96)]
    feats = compute_intraday_features(_bars(closes))
    for a in ("BUY", "SELL"):
        score, _ = intraday_alignment(a, feats)
        assert score < 60, f"chop must NOT trigger scalp scope ({a}={score})"


def test_alignment_handles_missing_pack():
    assert intraday_alignment("BUY", None) == (0, "")
    assert intraday_alignment("HOLD", {"trend": "UP"}) == (0, "")
