"""Tests for iter-32: autonomous deleverage + execution intelligence.

Covers:
  • Auto-deleverage sweep — disabled flag, cooldown, full fire
  • Liquidity scoring — score brackets / tier mapping / spread weighting
  • Smart router — MARKET vs LIMIT vs DEFER by tier, slice for big lots
  • TWAP/VWAP schedule — TWAP equal weights, VWAP sums to 1.0
  • Order-book pulse — classification thresholds
"""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock
from bson import ObjectId

from execution_intel.liquidity import (
    _spread_score, _tick_score, _session_score, _tier,
)
from execution_intel.smart_router import route as sor_route
from execution_intel.twap_vwap import build_schedule
from execution_intel.order_book import _classify, _depth_score


# ============================================================
# Liquidity scoring building blocks
# ============================================================
def test_spread_score_excellent_at_or_below_median():
    pts, ratio = _spread_score(1.0, 1.0)
    assert pts == 100
    assert ratio == pytest.approx(1.0)


def test_spread_score_zero_at_4x_median():
    pts, _ = _spread_score(4.0, 1.0)
    assert pts == 0


def test_spread_score_neutral_when_unknown():
    pts, ratio = _spread_score(None, None)
    assert pts == 60
    assert ratio is None


def test_tick_score_brackets():
    assert _tick_score(60) == 100
    assert _tick_score(1) == 0
    mid = _tick_score(30)
    assert 40 < mid < 60   # mid-range


def test_session_score_high_volume_window():
    pts, primary = _session_score({"primary": "london", "is_high_volume_window": True})
    assert pts == 100
    assert primary == "london"


def test_session_score_off_hours():
    pts, primary = _session_score({"primary": "off_hours"})
    assert pts == 30
    assert primary == "off_hours"


def test_tier_mapping():
    assert _tier(85) == "EXCELLENT"
    assert _tier(65) == "GOOD"
    assert _tier(45) == "FAIR"
    assert _tier(25) == "POOR"
    assert _tier(10) == "AVOID"


# ============================================================
# Smart Order Router
# ============================================================
def test_router_market_on_excellent_liquidity():
    out = sor_route(
        signal={"symbol": "BTCUSD", "action": "BUY", "lot_size": 0.01},
        liquidity={"tier": "EXCELLENT", "score": 90, "details": {"spread_ratio": 1.0, "session": "london"}},
    )
    assert out["order_type"] == "MARKET"
    assert out["slice_strategy"] == "NONE"


def test_router_limit_on_fair_liquidity():
    out = sor_route(
        signal={"symbol": "XAUUSD", "action": "BUY", "lot_size": 0.05},
        liquidity={"tier": "FAIR", "score": 50, "details": {"spread_ratio": 2.0, "session": "london"}},
    )
    assert out["order_type"] == "LIMIT"
    assert out["limit_offset_pips"] is not None
    # FAIR liquidity triggers slicing
    assert out["slice_strategy"] in ("TWAP", "VWAP")


def test_router_defers_on_avoid():
    out = sor_route(
        signal={"symbol": "XAUUSD", "action": "BUY", "lot_size": 0.05},
        liquidity={"tier": "AVOID", "score": 10, "details": {}},
    )
    assert out["order_type"] == "DEFER"


def test_router_slices_large_lot_even_on_excellent_liquidity():
    out = sor_route(
        signal={"symbol": "XAUUSD", "action": "BUY", "lot_size": 0.20},
        liquidity={"tier": "EXCELLENT", "score": 90, "details": {"session": "ny"}},
    )
    # Big lot → still slice, but order_type stays MARKET
    assert out["slice_strategy"] in ("TWAP", "VWAP")
    assert out["schedule_minutes"] > 0


def test_router_picks_vwap_in_london_ny():
    out = sor_route(
        signal={"symbol": "XAUUSD", "action": "BUY", "lot_size": 0.15},
        liquidity={"tier": "GOOD", "score": 70, "details": {"session": "london"}},
    )
    assert out["slice_strategy"] == "VWAP"


def test_router_picks_twap_off_hours():
    out = sor_route(
        signal={"symbol": "XAUUSD", "action": "BUY", "lot_size": 0.15},
        liquidity={"tier": "GOOD", "score": 70, "details": {"session": "off_hours"}},
    )
    assert out["slice_strategy"] == "TWAP"


# ============================================================
# TWAP / VWAP schedule
# ============================================================
def test_twap_equal_weights():
    sched = build_schedule(total_lot=0.20, duration_minutes=20,
                            strategy="TWAP", slices=4)
    assert sched["strategy"] == "TWAP"
    assert len(sched["slices"]) == 4
    for s in sched["slices"]:
        assert s["weight"] == pytest.approx(0.25)
    total = sum(s["lot"] for s in sched["slices"])
    assert total == pytest.approx(0.20)


def test_vwap_weights_sum_to_one():
    sched = build_schedule(total_lot=0.10, duration_minutes=12,
                            strategy="VWAP", slices=4)
    assert sched["strategy"] == "VWAP"
    assert sum(s["weight"] for s in sched["slices"]) == pytest.approx(1.0, rel=0.05)
    total = sum(s["lot"] for s in sched["slices"])
    assert total == pytest.approx(0.10, rel=0.01)


def test_schedule_fire_at_times_are_spaced():
    sched = build_schedule(total_lot=0.04, duration_minutes=20,
                            strategy="TWAP", slices=4)
    times = [datetime.fromisoformat(s["fire_at"]) for s in sched["slices"]]
    for i in range(1, len(times)):
        delta = (times[i] - times[i - 1]).total_seconds()
        assert delta == pytest.approx(5 * 60, rel=0.1)  # 5min each


def test_schedule_invalid_inputs():
    sched = build_schedule(total_lot=0, duration_minutes=10,
                            strategy="TWAP", slices=4)
    assert sched["slices"] == []


# ============================================================
# Order-book pulse
# ============================================================
def test_order_book_classify():
    assert _classify(30, 1.2) == "ACTIVE"
    assert _classify(10, 2.0) == "NORMAL"
    assert _classify(3, 1.5) == "THIN"
    assert _classify(0, None) == "FROZEN"


def test_depth_score_bounded():
    s_low = _depth_score(velocity=0, spread_ratio=4.0)
    s_high = _depth_score(velocity=60, spread_ratio=1.0)
    assert 0 <= s_low <= 100
    assert 0 <= s_high <= 100
    assert s_high > s_low


# ============================================================
# Auto-deleverage sweep
# ============================================================
@pytest.mark.asyncio
async def test_auto_deleverage_respects_disabled_flag(monkeypatch):
    from portfolio import auto_deleverage as ad
    monkeypatch.setenv("AUTO_DELEVERAGE_ENABLED", "false")
    out = await ad.sweep(MagicMock())
    assert out.get("disabled") is True


@pytest.mark.asyncio
async def test_auto_deleverage_skips_during_cooldown(monkeypatch):
    from portfolio import auto_deleverage as ad
    monkeypatch.setenv("AUTO_DELEVERAGE_ENABLED", "true")
    # Mock a recent cooldown stamp (5 min ago < 15 min cooldown)
    recent = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    acc = {"_id": ObjectId(), "user_id": "u1",
           "auto_deleverage_last_at": recent}
    on_cd = await ad._on_cooldown(acc)
    assert on_cd is True


@pytest.mark.asyncio
async def test_auto_deleverage_fires_when_needed(monkeypatch):
    """End-to-end: positions exist, snapshot says deleverage → broadcast + close."""
    from portfolio import auto_deleverage as ad
    monkeypatch.setenv("AUTO_DELEVERAGE_ENABLED", "true")

    acc_id = ObjectId()
    db = MagicMock()
    db.trades.distinct = AsyncMock(return_value=[str(acc_id)])
    db.accounts.find_one = AsyncMock(return_value={
        "_id": acc_id, "user_id": "u1", "equity": 8_000, "equity_hwm": 10_000,
    })
    db.accounts.update_one = AsyncMock()
    db.trades.find = MagicMock(return_value=MagicMock(
        to_list=AsyncMock(return_value=[
            {"_id": ObjectId(), "symbol": "XAUUSD", "lot_size": 0.05,
             "entry_price": 2050, "status": "open"},
        ])
    ))
    db.trades.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
    db.bot_configs.find_one = AsyncMock(return_value=None)
    fake_snap = {
        "needs_deleveraging": True,
        "triggers": ["hard_drawdown"],
        "actions": [{"kind": "close_trade", "trade_id": str(ObjectId()),
                     "symbol": "XAUUSD", "lot_size": 0.05,
                     "reason": "auto_deleverage_hard_drawdown"}],
        "drawdown": {"dd_pct": 20.0},
        "var": {"var_95_pct_equity": 1.0},
    }
    monkeypatch.setattr("portfolio.auto_deleverage.build_snapshot",
                        AsyncMock(return_value=fake_snap))

    broadcasts = []
    async def fake_broadcast(uid, evt, payload):
        broadcasts.append({"uid": uid, "evt": evt})
    monkeypatch.setattr("portfolio.auto_deleverage.ws_manager.broadcast",
                        fake_broadcast)
    monkeypatch.setattr("portfolio.auto_deleverage.send_telegram",
                        AsyncMock(return_value=True))

    out = await ad.sweep(db)
    assert out["triggered"] == 1
    assert out["closed"] == 1
    assert any(b["evt"] == "auto_deleverage" for b in broadcasts)


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.unit
