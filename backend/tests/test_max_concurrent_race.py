"""Regression: max_concurrent_trades enforcement is RACE-PROOF.

Previously, bot_runner read inflight ONCE before iterating symbols, and
the cooldown was in-memory only — hot reloads spawned duplicate
loop() tasks that ALL saw stale state and blew past the cap (user reported
12 open trades with cap=5).

Fix verified here:
1. execution.MT5BridgeEngine.execute() refuses to insert when fresh DB
   recount shows the cap is met.
2. PaperEngine.execute() applies the same guard.
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_mt5_engine_blocks_when_cap_reached(monkeypatch):
    """Engine MUST not insert a pending trade when inflight already >= cap."""
    from execution import MT5BridgeEngine

    fake_db = MagicMock()
    fake_db.trades.count_documents = AsyncMock(return_value=5)  # already at cap
    fake_db.trades.insert_one = AsyncMock()
    monkeypatch.setattr("execution.get_db", lambda: fake_db)

    engine = MT5BridgeEngine()
    result = await engine.execute(
        user_id="u1",
        account={"_id": "a1", "broker": "MT5"},
        signal={
            "symbol": "XAUUSD", "action": "SELL", "lot_size": 0.25,
            "entry_price": 3960.0, "stop_loss": 3970.0, "take_profit": 3940.0,
            "origin": "auto",
        },
        max_concurrent=5,
        cfg_account_id="acct_1",
    )
    assert result.get("blocked") == "max_concurrent_cap"
    assert result.get("inflight") == 5
    assert result.get("cap") == 5
    # Insertion MUST be skipped
    fake_db.trades.insert_one.assert_not_called()


@pytest.mark.asyncio
async def test_mt5_engine_allows_when_below_cap(monkeypatch):
    """Engine MUST still insert when inflight < cap."""
    from execution import MT5BridgeEngine

    fake_db = MagicMock()
    fake_db.trades.count_documents = AsyncMock(return_value=2)  # under cap
    fake_inserted = MagicMock()
    fake_inserted.inserted_id = "fakeid"
    fake_db.trades.insert_one = AsyncMock(return_value=fake_inserted)
    monkeypatch.setattr("execution.get_db", lambda: fake_db)
    monkeypatch.setattr("execution.ws_manager.broadcast", AsyncMock())

    async def passing_audit(**kw):  # noqa: ARG001
        return {"ok": True, "blocked_by": None, "audit": [],
                "evaluated_at": "2026-01-01T00:00:00+00:00", "context": {}}
    monkeypatch.setattr("execution.audit_pre_trade", passing_audit)

    engine = MT5BridgeEngine()
    result = await engine.execute(
        user_id="u1",
        account={"_id": "a1", "broker": "MT5", "mode": "live",
                 "equity": 10000.0, "balance": 10000.0, "free_margin": 9000.0},
        signal={
            "symbol": "XAUUSD", "action": "SELL", "lot_size": 0.05,
            "entry_price": 3960.0, "stop_loss": 3970.0, "take_profit": 3940.0,
            "origin": "auto",
        },
        max_concurrent=5,
        cfg_account_id="acct_1",
    )
    assert "blocked" not in result
    assert result.get("id") == "fakeid"
    fake_db.trades.insert_one.assert_awaited_once()


@pytest.mark.asyncio
async def test_mt5_engine_no_cap_arg_allows(monkeypatch):
    """Backward-compat: when max_concurrent isn't passed (e.g. manual UI
    trade execution), engine inserts without any cap check."""
    from execution import MT5BridgeEngine

    fake_db = MagicMock()
    fake_db.trades.count_documents = AsyncMock(return_value=999)  # would block if checked
    fake_inserted = MagicMock()
    fake_inserted.inserted_id = "fakeid"
    fake_db.trades.insert_one = AsyncMock(return_value=fake_inserted)
    monkeypatch.setattr("execution.get_db", lambda: fake_db)
    monkeypatch.setattr("execution.ws_manager.broadcast", AsyncMock())

    async def passing_audit(**kw):  # noqa: ARG001
        return {"ok": True, "blocked_by": None, "audit": [],
                "evaluated_at": "2026-01-01T00:00:00+00:00", "context": {}}
    monkeypatch.setattr("execution.audit_pre_trade", passing_audit)

    engine = MT5BridgeEngine()
    result = await engine.execute(
        user_id="u1",
        account={"_id": "a1", "broker": "MT5", "mode": "live",
                 "equity": 10000.0, "balance": 10000.0, "free_margin": 9000.0},
        signal={
            "symbol": "XAUUSD", "action": "BUY", "lot_size": 0.05,
            "entry_price": 3960.0, "stop_loss": 3950.0, "take_profit": 3980.0,
            "origin": "manual",
        },
    )
    assert "blocked" not in result
    # cap check is gated on max_concurrent > 0, so count_documents should NOT be called
    fake_db.trades.count_documents.assert_not_called()
