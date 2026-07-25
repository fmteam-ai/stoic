"""Tests for iter-45: loss post-mortem investigator + auto-tighten."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from bson import ObjectId

import loss_postmortem as lp


@pytest.mark.asyncio
async def test_eligibility_winner_skipped():
    db = MagicMock()
    trade = {"pnl": 50.0, "close_reason": "take_profit", "_id": ObjectId(),
             "user_id": "u1", "symbol": "XAUUSD"}
    eligible, _ = await lp._is_postmortem_eligible(db, trade)
    assert eligible is False


@pytest.mark.asyncio
async def test_eligibility_sl_hit():
    db = MagicMock()
    trade = {"pnl": -100.0, "close_reason": "stop_loss", "_id": ObjectId(),
             "user_id": "u1", "symbol": "XAUUSD"}
    eligible, reason = await lp._is_postmortem_eligible(db, trade)
    assert eligible is True
    assert reason == "sl_hit"


@pytest.mark.asyncio
async def test_eligibility_consecutive_loss():
    db = MagicMock()
    db.trades.find_one = AsyncMock(return_value={"pnl": -10.0})
    trade = {"pnl": -5.0, "close_reason": "manual", "_id": ObjectId(),
             "user_id": "u1", "symbol": "XAUUSD"}
    eligible, reason = await lp._is_postmortem_eligible(db, trade)
    assert eligible is True
    assert reason == "consecutive_loss"


@pytest.mark.asyncio
async def test_eligibility_single_loss_not_consecutive():
    db = MagicMock()
    db.trades.find_one = AsyncMock(return_value={"pnl": +30.0})  # prior was a winner
    trade = {"pnl": -5.0, "close_reason": "manual", "_id": ObjectId(),
             "user_id": "u1", "symbol": "XAUUSD"}
    eligible, trigger = await lp._is_postmortem_eligible(db, trade)
    # iter-127c: EVERY losing trade is eligible; single loss → 'loss' trigger
    assert eligible is True
    assert trigger == "loss"


def test_pattern_key_stable():
    trade = {"symbol": "XAUUSD", "action": "SELL"}
    signal = {"regime": {"regime": "TRANSITIONAL"}, "session": {"primary": "tokyo"}}
    assert lp._pattern_key(trade, signal) == "XAUUSD|TRANSITIONAL|TOKYO|SELL"


def test_pattern_key_tolerates_missing():
    trade = {"symbol": "BTCUSD", "action": "BUY"}
    signal = {}  # no regime/session
    assert lp._pattern_key(trade, signal) == "BTCUSD|UNKNOWN|OFF|BUY"


@pytest.mark.asyncio
async def test_maybe_record_skips_when_existing(monkeypatch):
    async def _tier(_uid):
        return "elite_ai"  # Loss Lab is Trader+ — bypass the billing gate
    monkeypatch.setattr("subscription_service.get_user_tier", _tier)
    db = MagicMock()
    tid = ObjectId()
    db.trades.find_one = AsyncMock(return_value={
        "_id": tid, "status": "closed", "pnl": -10, "close_reason": "stop_loss",
        "user_id": "u1", "symbol": "XAUUSD", "action": "SELL",
    })
    existing_doc = {"trade_id": str(tid), "narrative": {}}
    db.loss_postmortems.find_one = AsyncMock(return_value=existing_doc)
    db.loss_postmortems.insert_one = AsyncMock()
    out = await lp.maybe_record_postmortem(db, tid)
    # Returns the existing doc and does NOT insert a duplicate
    assert out == existing_doc
    db.loss_postmortems.insert_one.assert_not_called()


@pytest.mark.asyncio
async def test_auto_tighten_respects_opt_in_off():
    """If the user has NOT opted in, no adjustment is made even with ≥3 losses."""
    db = MagicMock()
    db.users.find_one = AsyncMock(return_value={"_id": "u1", "postmortem_settings": {"auto_tighten_enabled": False}})
    db.loss_postmortems.count_documents = AsyncMock(return_value=5)
    trade = {"user_id": "u1", "symbol": "XAUUSD", "account_id": None}
    out = await lp._maybe_autotighten(db, trade, {}, "XAUUSD|TRANSITIONAL|tokyo|SELL")
    assert out is None


@pytest.mark.asyncio
async def test_auto_tighten_below_threshold_no_adjustment():
    """≥3 losses required — 2 must NOT trigger adjustment."""
    db = MagicMock()
    db.users.find_one = AsyncMock(return_value={"_id": "u1", "postmortem_settings": {"auto_tighten_enabled": True}})
    db.loss_postmortems.count_documents = AsyncMock(return_value=2)
    trade = {"user_id": "u1", "symbol": "XAUUSD", "account_id": None}
    out = await lp._maybe_autotighten(db, trade, {}, "X|R|S|A")
    assert out is None


@pytest.mark.asyncio
async def test_auto_tighten_applies_when_eligible():
    db = MagicMock()
    db.users.find_one = AsyncMock(return_value={"_id": "u1", "postmortem_settings": {"auto_tighten_enabled": True}})
    db.loss_postmortems.count_documents = AsyncMock(return_value=4)
    db.guardrail_adjustments.find_one = AsyncMock(return_value=None)  # no cooldown active
    cfg_id = ObjectId()
    db.bot_configs.find_one = AsyncMock(return_value={"_id": cfg_id, "min_confidence_override": 65})
    db.bot_configs.update_one = AsyncMock()
    db.guardrail_adjustments.insert_one = AsyncMock()
    trade = {"user_id": "u1", "symbol": "XAUUSD", "account_id": None}
    out = await lp._maybe_autotighten(db, trade, {}, "X|R|S|A")
    assert out is not None
    # +5 bump on existing 65 → 70
    assert out["from"] == 65 and out["to"] == 70
    db.bot_configs.update_one.assert_awaited_once()
    db.guardrail_adjustments.insert_one.assert_awaited_once()


@pytest.mark.asyncio
async def test_auto_tighten_respects_cooldown():
    """If a recent adjustment exists for this pattern, do not re-tighten."""
    db = MagicMock()
    db.users.find_one = AsyncMock(return_value={"_id": "u1", "postmortem_settings": {"auto_tighten_enabled": True}})
    db.loss_postmortems.count_documents = AsyncMock(return_value=5)
    db.guardrail_adjustments.find_one = AsyncMock(return_value={"_id": ObjectId(), "to": 70})
    trade = {"user_id": "u1", "symbol": "XAUUSD", "account_id": None}
    out = await lp._maybe_autotighten(db, trade, {}, "X|R|S|A")
    assert out is None


@pytest.mark.asyncio
async def test_auto_tighten_clamps_at_95():
    """Bump must clamp at 95 even when current is already very high."""
    db = MagicMock()
    db.users.find_one = AsyncMock(return_value={"_id": "u1", "postmortem_settings": {"auto_tighten_enabled": True}})
    db.loss_postmortems.count_documents = AsyncMock(return_value=4)
    db.guardrail_adjustments.find_one = AsyncMock(return_value=None)
    cfg_id = ObjectId()
    db.bot_configs.find_one = AsyncMock(return_value={"_id": cfg_id, "min_confidence_override": 94})
    db.bot_configs.update_one = AsyncMock()
    db.guardrail_adjustments.insert_one = AsyncMock()
    trade = {"user_id": "u1", "symbol": "XAUUSD", "account_id": None}
    out = await lp._maybe_autotighten(db, trade, {}, "X|R|S|A")
    assert out is not None
    assert out["to"] == 95  # 94 + 5 → clamp at 95



# --------------------- AUTO-LOOSEN COUNTERPART ---------------------

@pytest.mark.asyncio
async def test_winner_with_no_prior_tighten_no_loosen():
    db = MagicMock()
    db.trades.find_one = AsyncMock(return_value={
        "_id": ObjectId(), "user_id": "u1", "symbol": "XAUUSD", "action": "BUY",
        "status": "closed", "pnl": 50, "signal_id": None,
    })
    db.users.find_one = AsyncMock(return_value={
        "_id": "u1", "postmortem_settings": {"auto_tighten_enabled": True},
    })
    db.guardrail_adjustments.find_one = AsyncMock(return_value=None)
    db.signals.find_one = AsyncMock(return_value=None)
    out = await lp.maybe_record_winner(db, ObjectId())
    assert out is None


@pytest.mark.asyncio
async def test_winner_loosen_requires_three_wins_after_tighten():
    """When prior tighten exists but only 2 wins followed, do NOT loosen yet."""
    db = MagicMock()
    db.trades.find_one = AsyncMock(return_value={
        "_id": ObjectId(), "user_id": "u1", "symbol": "XAUUSD", "action": "BUY",
        "status": "closed", "pnl": 50, "closed_at": "2026-06-26T05:00:00+00:00",
        "signal_id": None, "account_id": None,
    })
    db.users.find_one = AsyncMock(return_value={
        "_id": "u1", "postmortem_settings": {"auto_tighten_enabled": True},
    })
    db.signals.find_one = AsyncMock(return_value=None)
    tighten_doc = {"_id": ObjectId(), "direction": "tighten",
                   "created_at": "2026-06-25T10:00:00+00:00"}
    db.guardrail_adjustments.find_one = AsyncMock(side_effect=[tighten_doc, tighten_doc])
    db.trades.count_documents = AsyncMock(return_value=2)
    out = await lp.maybe_record_winner(db, ObjectId())
    assert out is None


@pytest.mark.asyncio
async def test_winner_loosen_applies_when_three_wins():
    db = MagicMock()
    db.trades.find_one = AsyncMock(return_value={
        "_id": ObjectId(), "user_id": "u1", "symbol": "XAUUSD", "action": "BUY",
        "status": "closed", "pnl": 50, "closed_at": "2026-06-26T05:00:00+00:00",
        "signal_id": None, "account_id": None,
    })
    db.users.find_one = AsyncMock(return_value={
        "_id": "u1", "postmortem_settings": {"auto_tighten_enabled": True},
    })
    db.signals.find_one = AsyncMock(return_value=None)
    tighten_doc = {"_id": ObjectId(), "direction": "tighten",
                   "created_at": "2026-06-25T10:00:00+00:00"}
    # Three find_one calls: last_tighten → last_any (still tighten) → recent_loosen=None
    db.guardrail_adjustments.find_one = AsyncMock(side_effect=[tighten_doc, tighten_doc, None])
    db.trades.count_documents = AsyncMock(return_value=3)
    cfg_id = ObjectId()
    db.bot_configs.find_one = AsyncMock(return_value={
        "_id": cfg_id, "min_confidence_override": 70,
    })
    db.bot_configs.update_one = AsyncMock()
    db.guardrail_adjustments.insert_one = AsyncMock()
    out = await lp.maybe_record_winner(db, ObjectId())
    assert out is not None
    assert out["from"] == 70 and out["to"] == 67  # 70 - 3 = 67
    assert out["direction"] == "loosen"
    db.bot_configs.update_one.assert_awaited_once()


@pytest.mark.asyncio
async def test_winner_loosen_clamps_at_floor_50():
    db = MagicMock()
    db.trades.find_one = AsyncMock(return_value={
        "_id": ObjectId(), "user_id": "u1", "symbol": "XAUUSD", "action": "BUY",
        "status": "closed", "pnl": 50, "closed_at": "2026-06-26T05:00:00+00:00",
        "signal_id": None, "account_id": None,
    })
    db.users.find_one = AsyncMock(return_value={
        "_id": "u1", "postmortem_settings": {"auto_tighten_enabled": True},
    })
    db.signals.find_one = AsyncMock(return_value=None)
    tighten_doc = {"_id": ObjectId(), "direction": "tighten",
                   "created_at": "2026-06-25T10:00:00+00:00"}
    db.guardrail_adjustments.find_one = AsyncMock(side_effect=[tighten_doc, tighten_doc, None])
    db.trades.count_documents = AsyncMock(return_value=5)
    db.bot_configs.find_one = AsyncMock(return_value={
        "_id": ObjectId(), "min_confidence_override": 52,
    })
    db.bot_configs.update_one = AsyncMock()
    db.guardrail_adjustments.insert_one = AsyncMock()
    out = await lp.maybe_record_winner(db, ObjectId())
    assert out is not None
    assert out["to"] == 50  # 52 - 3 = 49 → clamped to floor 50


@pytest.mark.asyncio
async def test_winner_loosen_skipped_when_already_loose():
    db = MagicMock()
    db.trades.find_one = AsyncMock(return_value={
        "_id": ObjectId(), "user_id": "u1", "symbol": "XAUUSD", "action": "BUY",
        "status": "closed", "pnl": 50, "signal_id": None,
    })
    db.users.find_one = AsyncMock(return_value={
        "_id": "u1", "postmortem_settings": {"auto_tighten_enabled": True},
    })
    db.signals.find_one = AsyncMock(return_value=None)
    tighten_doc = {"_id": ObjectId(), "direction": "tighten",
                   "created_at": "2026-06-25T10:00:00+00:00"}
    loosen_doc = {"_id": ObjectId(), "direction": "loosen",
                  "created_at": "2026-06-25T14:00:00+00:00"}
    db.guardrail_adjustments.find_one = AsyncMock(side_effect=[tighten_doc, loosen_doc])
    out = await lp.maybe_record_winner(db, ObjectId())
    assert out is None


@pytest.mark.asyncio
async def test_winner_loosen_respects_opt_in_off():
    db = MagicMock()
    db.trades.find_one = AsyncMock(return_value={
        "_id": ObjectId(), "user_id": "u1", "symbol": "XAUUSD", "action": "BUY",
        "status": "closed", "pnl": 50, "signal_id": None,
    })
    db.users.find_one = AsyncMock(return_value={
        "_id": "u1", "postmortem_settings": {"auto_tighten_enabled": False},
    })
    out = await lp.maybe_record_winner(db, ObjectId())
    assert out is None


@pytest.mark.asyncio
async def test_winner_loosen_skips_losses():
    db = MagicMock()
    db.trades.find_one = AsyncMock(return_value={
        "_id": ObjectId(), "user_id": "u1", "symbol": "XAUUSD",
        "status": "closed", "pnl": -10, "signal_id": None,
    })
    out = await lp.maybe_record_winner(db, ObjectId())
    assert out is None
