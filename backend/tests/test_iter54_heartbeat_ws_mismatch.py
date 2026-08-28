"""Regression test for iter-54 — heartbeat WebSocket broadcast must honour
the broker_account mismatch filter.

Bug: when the user attached two STOIC accounts (different bridge_tokens)
but both EAs ran on the same MT5 terminal (logged into Roboforex
account 67198987), the VT Markets row showed the Roboforex balance.

Root cause: `bridge_routes.heartbeat` filtered balance/equity to None
on the DB write when `payload.account_login != acc.account_number`, but
the WebSocket broadcast sent the raw `payload.balance` regardless. The
frontend's `account_heartbeat` handler then overwrote the correct DB-side
null with the wrong-account's value.

Fix: WS payload now uses the same `... if not mismatch else None` filter
as the DB write, and surfaces mismatch fields so the UI can warn live.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone


@pytest.mark.asyncio
async def test_heartbeat_ws_broadcast_nulls_balance_on_broker_mismatch():
    """When EA reports a different MT5 account than the STOIC row is
    configured for, the WebSocket broadcast must NOT leak the wrong
    account's balance/equity. It should send null + mismatch flags."""
    from routes import bridge_routes
    from routes.bridge_routes import BridgeHeartbeat as HeartbeatRequest

    # STOIC row is configured for account_number=1160638 (VT Markets)
    # but the EA is logged into 67198987 (Roboforex).
    acc = {
        "_id": "vtmarkets-account-id",
        "user_id": "user-abc",
        "account_number": "1160638",
        "bridge_token": "vtmarkets-token",
    }

    captured: dict = {}

    async def fake_broadcast(user_id, event_type, payload):
        captured["user_id"] = user_id
        captured["event_type"] = event_type
        captured["payload"] = payload

    db = MagicMock()
    db.accounts.find_one = AsyncMock(return_value=acc)
    db.accounts.update_one = AsyncMock()
    db.trades = MagicMock()
    db.trades.find = MagicMock()
    db.trades.count_documents = AsyncMock(return_value=0)
    db.trades.find.return_value.to_list = AsyncMock(return_value=[])
    db.trades.aggregate = MagicMock()
    db.trades.aggregate.return_value.to_list = AsyncMock(return_value=[])
    db.bridge_events = MagicMock()
    db.bridge_events.insert_one = AsyncMock()
    db.position_snapshots = MagicMock()
    db.position_snapshots.find_one = AsyncMock(return_value=None)

    payload = HeartbeatRequest(
        bridge_token="vtmarkets-token",
        balance=16596.41,        # the WRONG account's balance
        equity=16596.41,         # the WRONG account's balance
        open_positions=0,
        positions=[],
        account_login=67198987,  # ← MISMATCH: EA is on Roboforex acct
    )

    with patch.object(bridge_routes, "get_db", return_value=db), \
         patch.object(bridge_routes, "ws_manager") as ws_mgr, \
         patch.object(bridge_routes, "reconcile_account",
                      AsyncMock(return_value=None)):
        ws_mgr.broadcast = AsyncMock(side_effect=fake_broadcast)
        await bridge_routes.heartbeat(payload)

    assert captured.get("event_type") == "account_heartbeat", \
        f"expected account_heartbeat broadcast, got {captured}"
    p = captured["payload"]

    # The whole point of the bug fix:
    assert p["balance"] is None, \
        f"WS broadcast leaked wrong-account balance: {p['balance']}"
    assert p["equity"] is None, \
        f"WS broadcast leaked wrong-account equity: {p['equity']}"
    # And the mismatch flags must be surfaced so the UI can show the warning
    # live without waiting for a /accounts refresh.
    assert p["broker_account_mismatch"] is True
    assert p["broker_account_id_reported"] == 67198987
    assert "67198987" in (p.get("broker_account_mismatch_reason") or "")
    assert p["status"] == "disconnected"


@pytest.mark.asyncio
async def test_heartbeat_ws_broadcast_passes_through_when_no_mismatch():
    """Sanity check — when the EA is on the correct MT5 account, the WS
    broadcast carries the real balance/equity values through."""
    from routes import bridge_routes
    from routes.bridge_routes import BridgeHeartbeat as HeartbeatRequest

    acc = {
        "_id": "roboforex-account-id",
        "user_id": "user-abc",
        "account_number": "67198987",
        "bridge_token": "rf-token",
    }

    captured: dict = {}

    async def fake_broadcast(user_id, event_type, payload):
        captured["payload"] = payload

    db = MagicMock()
    db.accounts.find_one = AsyncMock(return_value=acc)
    db.accounts.update_one = AsyncMock()
    db.trades = MagicMock()
    db.trades.find = MagicMock()
    db.trades.count_documents = AsyncMock(return_value=0)
    db.trades.find.return_value.to_list = AsyncMock(return_value=[])
    db.trades.aggregate = MagicMock()
    db.trades.aggregate.return_value.to_list = AsyncMock(return_value=[])
    db.bridge_events = MagicMock()
    db.bridge_events.insert_one = AsyncMock()
    db.position_snapshots = MagicMock()
    db.position_snapshots.find_one = AsyncMock(return_value=None)

    payload = HeartbeatRequest(
        bridge_token="rf-token",
        balance=16596.41,
        equity=16596.41,
        open_positions=0,
        positions=[],
        account_login=67198987,  # matches acc.account_number
    )

    with patch.object(bridge_routes, "get_db", return_value=db), \
         patch.object(bridge_routes, "ws_manager") as ws_mgr, \
         patch.object(bridge_routes, "reconcile_account",
                      AsyncMock(return_value=None)):
        ws_mgr.broadcast = AsyncMock(side_effect=fake_broadcast)
        await bridge_routes.heartbeat(payload)

    p = captured["payload"]
    assert p["balance"] == 16596.41
    assert p["equity"] == 16596.41
    assert p["broker_account_mismatch"] is False
    assert p["status"] == "connected"


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.unit
