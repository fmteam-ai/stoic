"""#55 — engines blind to fast gold moves: live-price features, breakout live check, momentum override, fast slope,
stale-feature anti-tilt exemption, shadow compare; all behind engine_live_features_enabled (default OFF)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
pytestmark = pytest.mark.unit
BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intraday_features import compute_intraday_features  # noqa: E402
from strategy_engines import run_engine  # noqa: E402


def _m15(closes, t0=1_760_000_000, rng=0.6):
    import time
    # bars dated "today" (wall clock) so VWAP / range_pos are populated
    day = int(time.time()) // 86400 * 86400 + 7 * 3600
    out = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        out.append({"t": day + i * 900, "o": o, "h": max(o, c) + rng, "l": min(o, c) - rng, "c": c, "v": 10})
    return out


def _rally_then_flat():
    """Mirror of 16 Oct: long slow drift down (2 h slope negative), then a fast rally in the LAST bars."""
    closes = [4200 - 0.6 * i for i in range(60)]                       # 4200 → 4164.6 over 15 h
    closes += [4164 + 4.0 * i for i in range(1, 7)]                     # +24 in 1.5 h — last closed bar 4188
    return _m15(closes)


def test_live_price_drives_price_features_but_emas_stay_on_closed_bars():
    bars = _rally_then_flat()
    old = compute_intraday_features(bars)
    new = compute_intraday_features(bars, live_price=bars[-1]["c"] + 11.5)
    assert old["last_price"] == bars[-1]["c"] and not old["live_price_used"] and old["feature_price_gap"] == 0
    assert new["last_price"] == round(bars[-1]["c"] + 11.5, 2) and new["live_price_used"] and new["feature_price_gap"] == 11.5
    assert new["ema20"] == old["ema20"] and new["ema50"] == old["ema50"] and new["atr15"] == old["atr15"]   # closed-bar indicators
    assert new["vwap_dist_pct"] > old["vwap_dist_pct"] and new["range_pos_pct"] >= old["range_pos_pct"]
    assert new["donchian20"] == "BREAK_UP" and new["donchian_hi20"] == old["donchian_hi20"]
    assert new["stale_feature"] is True and compute_intraday_features(bars, live_price=bars[-1]["c"] + 0.1)["stale_feature"] is False


def test_fast_slope_confirms_trend_the_2h_slope_still_calls_flat():
    bars = _rally_then_flat()
    old = compute_intraday_features(bars)
    new = compute_intraday_features(bars, live_price=bars[-1]["c"] + 2.0)
    assert old["trend"] == "FLAT"                                                 # the 16 Oct blindness: slow stack never confirms
    assert new["ema20_slope_pct_1h"] > old["ema20_slope_pct_2h"] and new["trend_slow"] == "FLAT"
    assert new["trend"] == "UP"                                                   # fast slope + live price above EMA20


def test_momentum_override_buys_flat_but_strong_up_move_only_with_live_features():
    bars = _rally_then_flat()
    m5 = [{"c": 4186.0}, {"c": 4188.0}, {"c": 4190.0}]
    live = compute_intraday_features(bars, live_price=4190.5, m5_bars=m5)
    live["trend"] = "FLAT"                                                        # isolate rule (3) from rule (4)
    live["swing_structure"] = "MIXED"
    sig, note = run_engine("hf_scalp_fast", live)
    assert sig == "BUY" and "momentum override" in note
    assert live["m5_closes_rising"] and live["last_price"] - live["session_vwap"] >= 0.5 * live["atr15"]
    closed = compute_intraday_features(bars)
    sig_old, _ = run_engine("hf_scalp_fast", closed)
    assert sig_old != "BUY" or "momentum override" not in _                      # old pack: no override path
    live["swing_structure"] = "LH_LL"
    assert "momentum override" not in run_engine("hf_scalp_fast", live)[1]        # structure veto
    live["swing_structure"] = "MIXED"; live["m5_closes_rising"] = False
    assert "momentum override" not in run_engine("hf_scalp_fast", live)[1]        # needs 3 rising M5 closes


def test_breakout_requires_live_price_still_beyond_level():
    bars = _m15([4200 - 0.8 * i for i in range(70)])                                # steady slide → BREAK_DOWN on closed bars
    closed = compute_intraday_features(bars)
    assert closed["donchian20"] == "BREAK_DOWN" and run_engine("breakout_m15", closed)[0] == "SELL"
    rebounded = compute_intraday_features(bars, live_price=closed["donchian_lo20"] + 1.0)    # live price back inside
    sig, note = run_engine("breakout_m15", rebounded)
    assert sig is None and ("back inside" in note or "inside the Donchian" in note)
    shallow = compute_intraday_features(bars, live_price=closed["donchian_lo20"] - 0.05)      # beyond, but < 0.2×ATR15
    sig, note = run_engine("breakout_m15", shallow)
    assert sig is None and "0.2×ATR15" in note
    deep = compute_intraday_features(bars, live_price=closed["donchian_lo20"] - 0.2 * closed["atr15"] - 0.1)
    assert run_engine("breakout_m15", deep)[0] == "SELL"


def test_stale_feature_loss_does_not_count_for_anti_tilt():
    from streak_losses import classify_loss
    t = {"pnl": -40.0, "entry_price": 4160.2, "stop_loss": 4156.0, "lot_size": 0.1, "symbol": "XAUUSD", "action": "SELL"}
    assert classify_loss(dict(t))["counts"] is True
    c = classify_loss({**t, "stale_feature_entry": True})
    assert c["counts"] is False and "stale-feature" in c["why"]


def test_flag_default_off_and_shadow_compare_wiring():
    from models import BotConfigUpdate
    assert BotConfigUpdate().model_dump()["engine_live_features_enabled"] is False
    src = open(os.path.join(BACKEND, "ai_signals.py")).read()
    assert 'bool((freeze_cfg or {}).get("engine_live_features_enabled"))' in src
    assert "intraday_pack = (_pack_live or _pack_closed) if _live_on else _pack_closed" in src       # OFF → old features trade
    assert '"engine_shadow_compare": _cmp' in src and '"stale_feature_entry": bool((_pack_live or {}).get("stale_feature"))' in src
    ex = open(os.path.join(BACKEND, "execution.py")).read()
    assert '"stale_feature_entry": bool(signal.get("stale_feature_entry"))' in ex and '"engine_shadow_compare": signal.get("engine_shadow_compare")' in ex
    # legacy packs (no live price) must behave exactly as before: no override, no live breakout check
    bars = _rally_then_flat()
    closed = compute_intraday_features(bars)
    assert closed["live_price_used"] is False and closed["trend"] == closed["trend_slow"]
    for eng in ("hf_scalp", "hf_scalp_fast", "breakout_m15"):
        _, note = run_engine(eng, closed)
        assert "momentum override" not in note and "live price" not in note


def test_engine_replay_reports_old_vs_new_on_a_rally_window():
    from gold_trend_pulse.synthetic import synthetic_m1
    from scripts.engine_live_features_replay import markdown, replay_engines
    bars = synthetic_m1(days=6, seed=11)
    rep = replay_engines(bars)
    assert set(rep) == {"old", "new"} and "signals" in rep["new"] and "| arm | signals |" in markdown(rep, "t")
