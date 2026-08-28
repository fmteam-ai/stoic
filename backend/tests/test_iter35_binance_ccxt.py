"""Tests for iter-35: Binance Spot via CCXT execution engine + routes.

We mock `ccxt.async_support.binance` entirely — no real exchange calls.
Covers:
  - symbol normalization (BTCUSD ↔ BTC/USDT, ETH already-slashed passthrough)
  - testnet defence-in-depth (default-true; live requires both flags)
  - safety guardian wires in (refused trades persist a safety_block)
  - crypto-specific risk cap blocks oversized trades
  - max_concurrent guard
  - successful market order persists a trade with broker=BINANCE_SPOT
  - smart router: limit-hint → limit order placed
"""
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from bson import ObjectId

from crypto_bridge.binance_ccxt import (
    normalize_symbol, is_crypto_symbol, _is_testnet, _live_enabled,
)
from crypto_bridge.binance_engine import BinanceCCXTEngine, _smart_route


@pytest.fixture(autouse=True)
def _bypass_dispatch_gates(monkeypatch):
    """These are engine-mechanics tests — bypass the iter-122 entitlement
    gate (billing) so blocks under test surface deterministically."""
    async def _open(*a, **k):
        return None
    monkeypatch.setattr("entitlements.verify_execution_entitlement", _open)


# ============================================================
# Symbol normalization
# ============================================================
def test_normalize_symbol_internal_to_ccxt():
    assert normalize_symbol("BTCUSD") == "BTC/USDT"
    assert normalize_symbol("ETHUSD") == "ETH/USDT"
    assert normalize_symbol("SOLUSD") == "SOL/USDT"


def test_normalize_symbol_passthrough_when_already_slashed():
    assert normalize_symbol("BTC/USDT") == "BTC/USDT"


def test_normalize_symbol_handles_whitespace_and_case():
    assert normalize_symbol(" btcusd ") == "BTC/USDT"


def test_is_crypto_symbol_identifies_pairs():
    assert is_crypto_symbol("BTCUSD") is True
    assert is_crypto_symbol("ETH/USDT") is True
    assert is_crypto_symbol("XAUUSD") is False
    assert is_crypto_symbol("") is False


# ============================================================
# Testnet defence-in-depth
# ============================================================
def test_is_testnet_default_true():
    # account['testnet']=True → testnet regardless of others
    assert _is_testnet({"testnet": True}) is True


def test_is_testnet_when_live_flag_false():
    # No testnet flag, no live flag → testnet
    assert _is_testnet({}) is True


def test_is_testnet_global_kill_switch(monkeypatch):
    monkeypatch.delenv("BINANCE_LIVE_ENABLED", raising=False)
    # Even with account.live=True and account.testnet=False, global switch wins
    assert _is_testnet({"testnet": False, "live": True}) is True


def test_is_testnet_can_flip_to_live(monkeypatch):
    monkeypatch.setenv("BINANCE_LIVE_ENABLED", "true")
    assert _is_testnet({"testnet": False, "live": True}) is False


def test_live_enabled_reads_env(monkeypatch):
    monkeypatch.setenv("BINANCE_LIVE_ENABLED", "true")
    assert _live_enabled() is True
    monkeypatch.setenv("BINANCE_LIVE_ENABLED", "false")
    assert _live_enabled() is False


# ============================================================
# Smart Router
# ============================================================
def test_smart_router_defaults_to_market():
    order_type, limit_px = _smart_route({"symbol": "BTCUSD", "action": "BUY"})
    assert order_type == "market"
    assert limit_px is None


def test_smart_router_picks_limit_when_hinted():
    order_type, limit_px = _smart_route({
        "symbol": "BTCUSD", "action": "BUY",
        "execution_hint": "limit", "limit_price": 60000.0,
    })
    assert order_type == "limit"
    assert limit_px == 60000.0


# ============================================================
# Execution engine — full pipeline (mocked exchange + DB)
# ============================================================
def _make_signal(**over):
    sig = {
        "symbol": "BTCUSD", "action": "BUY",
        "lot_size": 0.001, "entry_price": 60000.0,
        "stop_loss": 59700.0, "take_profit": 60900.0,
        "origin": "manual",
    }
    sig.update(over)
    return sig


def _make_account(**over):
    acc = {
        "_id": ObjectId(), "user_id": "u1", "kind": "binance",
        "testnet": True, "live": False, "mode": "paper",
        "broker": "BINANCE_SPOT",
        "balance": 10000.0, "equity": 10000.0, "free_margin": 10000.0,
        "creds": {"api_key": {"ciphertext": "x", "nonce": "x", "v": 1},
                  "api_secret": {"ciphertext": "x", "nonce": "x", "v": 1}},
        "account_type": "standard",
    }
    acc.update(over)
    return acc


@pytest.mark.asyncio
async def test_engine_blocks_when_max_concurrent_hit():
    db = MagicMock()
    db.trades.count_documents = AsyncMock(return_value=3)

    with patch("crypto_bridge.binance_engine.get_db", return_value=db):
        result = await BinanceCCXTEngine().execute(
            user_id="u1", account=_make_account(),
            signal=_make_signal(), max_concurrent=2,
            cfg_account_id="acc1",
        )
    assert result["blocked"] == "max_concurrent_cap"
    assert result["inflight"] == 3


@pytest.mark.asyncio
async def test_engine_routes_through_safety_guardian():
    """Safety guardian refusal → engine returns 'safety_guardian' block, persists block."""
    db = MagicMock()
    db.trades.count_documents = AsyncMock(return_value=0)
    db.safety_blocks.insert_one = AsyncMock()

    fake_safety = {"ok": False, "blocked_by": "equity_known",
                   "audit": [{"name": "equity_known", "ok": False}],
                   "context": {"equity": 0}}
    with patch("crypto_bridge.binance_engine.get_db", return_value=db), \
         patch("crypto_bridge.binance_engine.audit_pre_trade",
               new=AsyncMock(return_value=fake_safety)):
        result = await BinanceCCXTEngine().execute(
            user_id="u1", account=_make_account(equity=0, balance=0),
            signal=_make_signal(), cfg_account_id="acc1",
        )
    assert result["blocked"] == "safety_guardian"
    assert result["safety_blocked_by"] == "equity_known"
    db.safety_blocks.insert_one.assert_awaited_once()


@pytest.mark.asyncio
async def test_engine_blocks_when_crypto_risk_cap_exceeded(monkeypatch):
    """Even if MT5-style guardian passes, the crypto-specific 0.5% cap can refuse."""
    monkeypatch.setenv("CRYPTO_MAX_RISK_PCT_PER_TRADE", "0.5")
    db = MagicMock()
    db.trades.count_documents = AsyncMock(return_value=0)

    # SL distance $1000 × amount 1 BTC = $1000 risk; 0.5% of $10k = $50 cap → blocked
    big_signal = _make_signal(lot_size=1.0, entry_price=60000, stop_loss=59000)

    with patch("crypto_bridge.binance_engine.get_db", return_value=db), \
         patch("crypto_bridge.binance_engine.audit_pre_trade",
               new=AsyncMock(return_value={"ok": True, "audit": [], "context": {}})):
        result = await BinanceCCXTEngine().execute(
            user_id="u1", account=_make_account(),
            signal=big_signal, cfg_account_id="acc1",
        )
    assert result["blocked"] == "crypto_risk_cap"
    assert result["risk_usd"] > result["cap_usd"]


@pytest.mark.asyncio
async def test_engine_places_market_order_and_persists_trade():
    db = MagicMock()
    db.trades.count_documents = AsyncMock(return_value=0)
    inserted_id = ObjectId()
    db.trades.insert_one = AsyncMock(return_value=MagicMock(inserted_id=inserted_id))
    db.trades.update_one = AsyncMock()

    fake_client = MagicMock()
    fake_client.create_market_order = AsyncMock(return_value={
        "id": "ORDER123", "status": "closed", "average": 60010.5, "price": 60010.5,
    })
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)

    fake_ws = MagicMock()
    fake_ws.broadcast = AsyncMock()

    with patch("crypto_bridge.binance_engine.get_db", return_value=db), \
         patch("crypto_bridge.binance_engine.audit_pre_trade",
               new=AsyncMock(return_value={"ok": True, "audit": [], "context": {}})), \
         patch("crypto_bridge.binance_engine.BinanceClient", return_value=fake_client), \
         patch("crypto_bridge.binance_engine.ws_manager", fake_ws):
        result = await BinanceCCXTEngine().execute(
            user_id="u1", account=_make_account(),
            signal=_make_signal(), cfg_account_id="acc1",
        )

    fake_client.create_market_order.assert_awaited_once()
    db.trades.insert_one.assert_awaited_once()
    persisted = db.trades.insert_one.await_args[0][0]
    assert persisted["broker"] == "BINANCE_SPOT"
    assert persisted["broker_kind"] == "binance"
    assert persisted["source"] == "binance"
    assert persisted["exchange_symbol"] == "BTC/USDT"
    assert persisted["exchange_order_id"] == "ORDER123"
    assert persisted["entry_price"] == 60010.5
    assert persisted["status"] == "open"
    assert result["id"] == str(inserted_id)
    fake_ws.broadcast.assert_awaited()


@pytest.mark.asyncio
async def test_engine_places_limit_order_when_hinted():
    db = MagicMock()
    db.trades.count_documents = AsyncMock(return_value=0)
    db.trades.insert_one = AsyncMock(return_value=MagicMock(inserted_id=ObjectId()))
    db.trades.update_one = AsyncMock()

    fake_client = MagicMock()
    fake_client.create_limit_order = AsyncMock(return_value={
        "id": "LIM456", "status": "open", "price": 59500.0,
    })
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)

    fake_ws = MagicMock()
    fake_ws.broadcast = AsyncMock()

    sig = _make_signal(execution_hint="limit", limit_price=59500.0)
    with patch("crypto_bridge.binance_engine.get_db", return_value=db), \
         patch("crypto_bridge.binance_engine.audit_pre_trade",
               new=AsyncMock(return_value={"ok": True, "audit": [], "context": {}})), \
         patch("crypto_bridge.binance_engine.BinanceClient", return_value=fake_client), \
         patch("crypto_bridge.binance_engine.ws_manager", fake_ws):
        result = await BinanceCCXTEngine().execute(
            user_id="u1", account=_make_account(),
            signal=sig, cfg_account_id="acc1",
        )

    fake_client.create_limit_order.assert_awaited_once_with(
        "BTC/USDT", "buy", 0.001, 59500.0,
    )
    persisted = db.trades.insert_one.await_args[0][0]
    assert persisted["status"] == "pending"  # limit order not filled yet
    assert result.get("exchange_order_id") == "LIM456"


@pytest.mark.asyncio
async def test_engine_handles_exchange_error_gracefully():
    db = MagicMock()
    db.trades.count_documents = AsyncMock(return_value=0)

    fake_client = MagicMock()
    fake_client.create_market_order = AsyncMock(side_effect=Exception("insufficient funds"))
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    # __aexit__ must NOT return truthy or it would swallow the exception
    fake_client.__aexit__ = AsyncMock(return_value=False)

    with patch("crypto_bridge.binance_engine.get_db", return_value=db), \
         patch("crypto_bridge.binance_engine.audit_pre_trade",
               new=AsyncMock(return_value={"ok": True, "audit": [], "context": {}})), \
         patch("crypto_bridge.binance_engine.BinanceClient", return_value=fake_client):
        result = await BinanceCCXTEngine().execute(
            user_id="u1", account=_make_account(),
            signal=_make_signal(), cfg_account_id="acc1",
        )

    assert result["blocked"] == "exchange_error"
    assert "insufficient funds" in result["error"]


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.unit
