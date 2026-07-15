"""Iter 25g — External (manual) trade notifications are clearly distinguished.

When an external-deal fires (user opened a position on MT5 outside STOIC,
or another EA did), the Telegram alert must:
  • use a different title — "📌 Manual Trade Detected" (not "🟢 Trade Opened")
  • use a different event_type — "external_trade_opened" (so the user can
    mute these without muting their bot's own trades)
  • include a clear "NOT opened by STOIC" disclaimer line

This was driven by user feedback: a "🟢 Trade Opened · BTCUSD" Telegram
made the user think the bot had opened a trade outside its config when in
reality the trade came from another MT5 client.
"""
import asyncio
from unittest.mock import patch, AsyncMock


def test_notify_trade_opened_external_uses_distinct_title():
    from notifier import notify_trade_opened
    captured = {}

    async def _fake_send(user_id, event_type, title, lines):
        captured["event_type"] = event_type
        captured["title"] = title
        captured["lines"] = lines
        return True

    async def _run():
        with patch("notifier.send_telegram", new=_fake_send), \
             patch("notifier._is_plausible_trade", new=AsyncMock(return_value=True)):
            await notify_trade_opened("user-x", {
                "symbol": "BTCUSD", "action": "BUY",
                "lot_size": 0.05, "entry_price": 62500.0,
                "stop_loss": 0.0, "take_profit": 0.0,
                "origin": "external", "mt5_ticket": 70000123,
            })

    asyncio.run(_run())

    assert captured["event_type"] == "external_trade_opened"
    assert "Manual Trade Detected" in captured["title"]
    assert "📌" in captured["title"]
    joined = " ".join(captured["lines"])
    assert "NOT opened by STOIC" in joined


def test_notify_trade_opened_bot_keeps_classic_format():
    from notifier import notify_trade_opened
    captured = {}

    async def _fake_send(user_id, event_type, title, lines):
        captured["event_type"] = event_type
        captured["title"] = title
        captured["lines"] = lines
        return True

    async def _run():
        with patch("notifier.send_telegram", new=_fake_send), \
             patch("notifier._is_plausible_trade", new=AsyncMock(return_value=True)):
            await notify_trade_opened("user-x", {
                "symbol": "XAUUSD", "action": "BUY",
                "lot_size": 0.2, "entry_price": 4100.0,
                "stop_loss": 4115.0, "take_profit": 4070.0,
                "origin": "auto", "mt5_ticket": 70000124,
            })

    asyncio.run(_run())

    assert captured["event_type"] == "trade_opened"
    assert "Trade Opened" in captured["title"]
    assert "Origin: auto" in " ".join(captured["lines"])


def test_external_open_flag_alone_triggers_manual_path():
    """Even if `origin` isn't set, `external_open=True` should route to the
    manual-detected channel — covers older trades that have the flag but
    no origin field."""
    from notifier import notify_trade_opened
    captured = {}

    async def _fake_send(user_id, event_type, title, lines):
        captured["event_type"] = event_type
        return True

    async def _run():
        with patch("notifier.send_telegram", new=_fake_send), \
             patch("notifier._is_plausible_trade", new=AsyncMock(return_value=True)):
            await notify_trade_opened("user-x", {
                "symbol": "BTCUSD", "action": "BUY",
                "lot_size": 0.05, "entry_price": 62500.0,
                "external_open": True, "mt5_ticket": 70000125,
            })

    asyncio.run(_run())
    assert captured["event_type"] == "external_trade_opened"


def test_external_trade_opened_in_default_alerts_table():
    """The new event key must be in DEFAULT_ALERTS so users see it on the
    Notifications page and can toggle it from the start."""
    from routes.notification_routes import DEFAULT_ALERTS
    assert "external_trade_opened" in DEFAULT_ALERTS


def test_is_plausible_trade_rejects_dict_without_id():
    """The DB-presence guard must reject any dict that wasn't actually
    persisted — this is the safety net that catches accidental/synthetic
    notify_* invocations (e.g. an agent's ad-hoc test push)."""
    from notifier import _is_plausible_trade

    async def _run():
        # No _id and no id → reject immediately
        return await _is_plausible_trade({
            "symbol": "BTCUSD", "action": "BUY",
            "lot_size": 0.05, "entry_price": 62500.0,
            "mt5_ticket": 696483514,  # even a REAL-looking ticket
        })
    assert asyncio.run(_run()) is False


def test_is_plausible_trade_rejects_id_not_in_db():
    """Even with an _id that LOOKS valid, if the trade isn't persisted in
    Mongo we refuse — closes the door on stale/replayed payloads."""
    from bson import ObjectId
    from notifier import _is_plausible_trade

    fake_oid = ObjectId()  # never inserted

    async def _run():
        return await _is_plausible_trade({
            "_id": fake_oid, "symbol": "BTCUSD", "action": "BUY",
            "lot_size": 0.05, "entry_price": 62500.0,
            "mt5_ticket": 696483514,
        })
    assert asyncio.run(_run()) is False
