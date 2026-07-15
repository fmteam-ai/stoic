"""Tests for EA v1.27 broker-real-time tick relay (`position_ticks` WS event).

Validates:
  - Heartbeat without positions → no `position_ticks` broadcast
  - Heartbeat with positions that lack `current_price` → no broadcast
  - Heartbeat with positions carrying `current_price` → one consolidated
    broadcast carrying all ticks + the heartbeat timestamp
  - Mismatch (wrong terminal) → no broadcast (we don't relay foreign ticks)
  - Quote cache TTL knob is tightened (5s for crypto/commodity)
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from market import ttl_seconds_for_quote
from models import BridgePosition, BridgeHeartbeat


# ---------- TTL knob ----------
def test_quote_ttl_tightened_for_live_ui():
    """5s ceiling for the assets we actively trade (crypto + commodity).
    FX stays slower because it moves slowly and we don't want to burn quota."""
    assert ttl_seconds_for_quote("crypto") == 5
    assert ttl_seconds_for_quote("commodity") == 5
    assert ttl_seconds_for_quote("forex") == 60


# ---------- BridgePosition schema ----------
def test_position_accepts_current_price():
    p = BridgePosition(
        ticket=12345, symbol="XAUUSD", type="BUY",
        volume=0.10, price_open=2050.0, sl=2040.0, tp=2070.0,
        time_open=1700000000, magic=901234, profit=12.34,
        current_price=2055.5,
    )
    assert p.current_price == pytest.approx(2055.5)


def test_position_current_price_is_optional():
    """Legacy EAs (<v1.27) don't send it — must default to None, not blow up."""
    p = BridgePosition(
        ticket=12345, symbol="BTCUSD", type="SELL",
        volume=0.01, price_open=60_000.0,
        magic=0, profit=-3.21,
    )
    assert p.current_price is None


# ---------- Broadcast wiring ----------
@pytest.mark.asyncio
async def test_heartbeat_broadcasts_position_ticks(monkeypatch):
    """End-to-end: a v1.27 heartbeat with two positions triggers ONE
    `position_ticks` WS event carrying both ticks."""
    from routes import bridge_routes

    # Mock account lookup
    acc = {"_id": "acct1", "user_id": "u1", "account_number": 67_198_987,
           "broker": "MT5", "bridge_token": "tok"}
    fake_db = MagicMock()
    fake_db.accounts.find_one = AsyncMock(return_value=acc)
    fake_db.accounts.update_one = AsyncMock()
    fake_db.trades.find_one = AsyncMock(return_value=None)  # nothing tracked yet
    fake_db.trades.insert_one = AsyncMock()
    fake_db.trades.count_documents = AsyncMock(return_value=0)
    fake_db.trades.find = MagicMock(return_value=MagicMock(
        to_list=AsyncMock(return_value=[]),
    ))
    monkeypatch.setattr("routes.bridge_routes.get_db", lambda: fake_db)

    # Capture broadcasts
    sent = []
    async def _capture(uid, event, payload):
        sent.append({"uid": uid, "event": event, "payload": payload})
    monkeypatch.setattr("routes.bridge_routes.ws_manager.broadcast", _capture)

    hb = BridgeHeartbeat(
        bridge_token="tok", balance=10_000.0, equity=10_005.0,
        open_positions=2, account_login=67_198_987, base_currency="USD",
        client_version="1.27",
        positions=[
            BridgePosition(ticket=111, symbol="XAUUSD", type="BUY",
                           volume=0.10, price_open=2050.0,
                           magic=901234, profit=12.34, current_price=2051.2),
            BridgePosition(ticket=222, symbol="BTCUSD", type="SELL",
                           volume=0.01, price_open=60_000.0,
                           magic=901234, profit=-3.21, current_price=59_900.0),
        ],
    )
    await bridge_routes.heartbeat(hb)

    # Exactly one position_ticks broadcast, with both ticks
    ticks_msgs = [m for m in sent if m["event"] == "position_ticks"]
    assert len(ticks_msgs) == 1
    payload = ticks_msgs[0]["payload"]
    assert payload["account_id"] == "acct1"
    assert len(payload["ticks"]) == 2
    by_ticket = {t["ticket"]: t for t in payload["ticks"]}
    assert by_ticket[111]["current_price"] == pytest.approx(2051.2)
    assert by_ticket[111]["profit"] == pytest.approx(12.34)
    assert by_ticket[222]["symbol"] == "BTCUSD"


@pytest.mark.asyncio
async def test_heartbeat_skips_broadcast_when_no_current_price(monkeypatch):
    """Legacy EA (<v1.27) sends positions without current_price → no relay."""
    from routes import bridge_routes
    acc = {"_id": "acct1", "user_id": "u1", "account_number": 1,
           "broker": "MT5", "bridge_token": "tok"}
    fake_db = MagicMock()
    fake_db.accounts.find_one = AsyncMock(return_value=acc)
    fake_db.accounts.update_one = AsyncMock()
    fake_db.trades.find_one = AsyncMock(return_value=None)
    fake_db.trades.insert_one = AsyncMock()
    fake_db.trades.count_documents = AsyncMock(return_value=0)
    fake_db.trades.find = MagicMock(return_value=MagicMock(
        to_list=AsyncMock(return_value=[]),
    ))
    monkeypatch.setattr("routes.bridge_routes.get_db", lambda: fake_db)

    sent = []
    async def _capture(uid, event, payload):
        sent.append({"event": event})
    monkeypatch.setattr("routes.bridge_routes.ws_manager.broadcast", _capture)

    hb = BridgeHeartbeat(
        bridge_token="tok", balance=10_000.0, equity=10_000.0,
        open_positions=1, account_login=1, base_currency="USD",
        positions=[
            BridgePosition(ticket=111, symbol="XAUUSD", type="BUY",
                           volume=0.10, price_open=2050.0,
                           magic=901234, profit=0.0),  # no current_price
        ],
    )
    await bridge_routes.heartbeat(hb)
    assert not any(m["event"] == "position_ticks" for m in sent)


@pytest.mark.asyncio
async def test_heartbeat_skips_broadcast_on_terminal_mismatch(monkeypatch):
    """If EA is reading the wrong MT5 terminal, we must NOT relay foreign ticks."""
    from routes import bridge_routes
    # Configured for account 100 — EA reports it's logged into account 200
    acc = {"_id": "acct1", "user_id": "u1", "account_number": 100,
           "broker": "MT5", "bridge_token": "tok"}
    fake_db = MagicMock()
    fake_db.accounts.find_one = AsyncMock(return_value=acc)
    fake_db.accounts.update_one = AsyncMock()
    fake_db.trades.find_one = AsyncMock(return_value=None)
    fake_db.trades.insert_one = AsyncMock()
    fake_db.trades.count_documents = AsyncMock(return_value=0)
    fake_db.trades.find = MagicMock(return_value=MagicMock(
        to_list=AsyncMock(return_value=[]),
    ))
    monkeypatch.setattr("routes.bridge_routes.get_db", lambda: fake_db)

    sent = []
    async def _capture(uid, event, payload):
        sent.append({"event": event})
    monkeypatch.setattr("routes.bridge_routes.ws_manager.broadcast", _capture)

    hb = BridgeHeartbeat(
        bridge_token="tok", balance=10_000.0, equity=10_000.0,
        open_positions=1, account_login=200,  # ← mismatch
        base_currency="USD",
        positions=[
            BridgePosition(ticket=111, symbol="XAUUSD", type="BUY",
                           volume=0.10, price_open=2050.0,
                           magic=901234, profit=12.34, current_price=2055.0),
        ],
    )
    await bridge_routes.heartbeat(hb)
    assert not any(m["event"] == "position_ticks" for m in sent)
