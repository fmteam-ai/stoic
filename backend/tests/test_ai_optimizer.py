"""Regression tests — AI Strategy Optimizer pure logic (no LLM, no network)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_optimizer import compute_trade_stats, validate_recommendations, _clamp


def _t(pnl, symbol="XAUUSD", action="BUY", opened="2026-07-02T09:00:00+00:00",
       closed="2026-07-02T10:00:00+00:00", reason="take_profit"):
    return {"pnl": pnl, "base_symbol": symbol, "action": action,
            "opened_at": opened, "closed_at": closed, "close_reason": reason}


def test_stats_basic():
    trades = [_t(100), _t(-50), _t(30), _t(-20), _t(60)]
    s = compute_trade_stats(trades)
    assert s["total_trades"] == 5
    assert s["wins"] == 3 and s["losses"] == 2
    assert s["win_rate"] == 60.0
    assert s["total_pnl"] == 120.0
    assert s["profit_factor"] == round(190 / 70, 2)
    assert s["by_symbol"]["XAUUSD"]["trades"] == 5
    assert s["by_session"]["london"]["trades"] == 5  # 09:00 UTC = london


def test_stats_empty_and_streak():
    assert compute_trade_stats([])["total_trades"] == 0
    # losing streak of 3 (chronological by closed_at)
    trades = [
        _t(-10, closed="2026-07-02T10:00:00+00:00"),
        _t(-10, closed="2026-07-02T11:00:00+00:00"),
        _t(-10, closed="2026-07-02T12:00:00+00:00"),
        _t(50,  closed="2026-07-02T13:00:00+00:00"),
    ]
    assert compute_trade_stats(trades)["worst_losing_streak"] == 3


def test_clamp():
    assert _clamp("min_confidence_override", 120) == 95
    assert _clamp("min_confidence_override", -5) == 0
    assert _clamp("partial_close_fraction", 0.95) == 0.9
    assert _clamp("trailing_enabled", 1) is True
    assert _clamp("profit_taking_mode", "WIN_RATE") == "win_rate"
    assert _clamp("profit_taking_mode", "bogus") is None
    assert _clamp("sl_cooldown_minutes", "abc") is None


def test_validate_recommendations_whitelist():
    cfg = {"min_confidence_override": 60, "active_preset": "sniper", "active": True,
           "sl_cooldown_enabled": False}
    raw = [
        # valid config change
        {"type": "config_change", "field": "min_confidence_override", "to": 70,
         "reason": "r", "expected_impact": "i"},
        # hallucinated field → dropped
        {"type": "config_change", "field": "evil_field", "to": 1, "reason": "r"},
        # no-op (same value) → dropped
        {"type": "config_change", "field": "min_confidence_override", "to": 60, "reason": "r"},
        # preset switch to current preset → dropped
        {"type": "preset_switch", "preset_key": "sniper", "reason": "r"},
        # valid preset switch
        {"type": "preset_switch", "preset_key": "scalper", "reason": "r"},
        # valid pause (bot active)
        {"type": "pause_bot", "reason": "r"},
    ]
    out = validate_recommendations(raw, cfg)
    types = [r["type"] for r in out]
    assert types == ["config_change", "preset_switch", "pause_bot"]
    assert out[0]["from"] == 60 and out[0]["to"] == 70
    assert out[1]["preset_key"] == "scalper"
    assert all(r["status"] == "pending" and r["id"] for r in out)


def test_pause_dropped_when_bot_inactive():
    cfg = {"active": False}
    out = validate_recommendations([{"type": "pause_bot", "reason": "r"}], cfg)
    assert out == []


def test_max_four_recommendations():
    cfg = {"active": True}
    raw = [{"type": "pause_bot", "reason": f"r{i}"} for i in range(6)]
    # only first is kept as valid pause; but cap check via config changes
    raw2 = [{"type": "config_change", "field": "trade_of_day_cap", "to": i + 2, "reason": "r"}
            for i in range(6)]
    assert len(validate_recommendations(raw2, cfg)) <= 4
