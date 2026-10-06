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
import sys
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
    monkeypatch.setenv("CRYPTO_LIVE_TRADING_ENABLED", "true")   # A13-1: both switches required
    assert _is_testnet({"testnet": False, "live": True}) is False


def test_live_enabled_reads_env(monkeypatch):
    monkeypatch.setenv("CRYPTO_LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("BINANCE_LIVE_ENABLED", "true")
    assert _live_enabled() is True
    monkeypatch.setenv("BINANCE_LIVE_ENABLED", "false")
    assert _live_enabled() is False
    monkeypatch.setenv("BINANCE_LIVE_ENABLED", "true")
    monkeypatch.delenv("CRYPTO_LIVE_TRADING_ENABLED", raising=False)
    assert _live_enabled() is False                               # exchange flag alone is NOT enough


def _mock_db():
    """MagicMock db with REAL in-memory collections for the A13 reservation / intent paths."""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "unit"))
    from fake_mongo import FakeCollection
    db = MagicMock()
    for name in ("risk_reservations", "account_reservation_locks", "execution_intents", "bot_configs"):
        setattr(db, name, FakeCollection())
    from types import SimpleNamespace
    db.trades.update_one = AsyncMock(return_value=SimpleNamespace(matched_count=1, modified_count=1))
    db.trades.find_one = AsyncMock(return_value=None)
    return db


@pytest.fixture(autouse=True)
def _paper_authority_passes():
    """Paper-account engine tests: the canonical trading-authority gate is covered by its own suite."""
    with patch("trading_authority.gate_or_block", new=AsyncMock(return_value=None)):
        yield


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
    db = _mock_db()
    # N97-1 — a MANUAL signal is bound by the hard total cap (auto cap + buffer 2), not the auto cap
    db.trades.count_documents = AsyncMock(return_value=5)

    with patch("crypto_bridge.binance_engine.get_db", return_value=db):
        result = await BinanceCCXTEngine().execute(
            user_id="u1", account=_make_account(),
            signal=_make_signal(), max_concurrent=2,
            cfg_account_id="acc1",
        )
    assert result["blocked"] == "max_concurrent_cap"
    assert result["total_inflight"] == 5 and result["total_cap"] == 4


@pytest.mark.asyncio
async def test_engine_routes_through_safety_guardian():
    """Safety guardian refusal → engine returns 'safety_guardian' block, persists block."""
    db = _mock_db()
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
    db = _mock_db()
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
    db = _mock_db()
    db.trades.count_documents = AsyncMock(return_value=0)
    inserted_id = ObjectId()
    db.trades.insert_one = AsyncMock(return_value=MagicMock(inserted_id=inserted_id))

    fake_client = MagicMock()
    fake_client.create_market_order = AsyncMock(return_value={
        "id": "ORDER123", "status": "closed", "average": 60010.5, "price": 60010.5,
        "filled": 0.001, "fees": [{"currency": "BTC", "cost": 0.000001}],         # N97-8 — base-asset fee
    })
    fake_client.place_oco_protection = AsyncMock(return_value={"list_id": "L1"})   # A13 P0-01
    fake_client.amount_to_precision = lambda sym, amt: float(amt)
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
    assert fake_client.create_market_order.await_args.kwargs["client_order_id"].startswith("stoic-")
    fake_client.place_oco_protection.assert_awaited_once()                        # exchange-side SL/TP
    assert fake_client.place_oco_protection.await_args.args[2] == pytest.approx(0.000999)   # filled − base fee
    db.trades.insert_one.assert_awaited_once()
    persisted = db.trades.insert_one.await_args[0][0]
    assert persisted["client_order_id"] == fake_client.create_market_order.await_args.kwargs["client_order_id"]
    assert persisted["execution_intent_id"].startswith("xin_") and persisted["reservation_id"]
    assert result["protection"]["status"] == "placed"
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
    db = _mock_db()
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

    fake_client.create_limit_order.assert_awaited_once()
    assert fake_client.create_limit_order.await_args.args == ("BTC/USDT", "buy", 0.001, 59500.0)
    assert fake_client.create_limit_order.await_args.kwargs["client_order_id"].startswith("stoic-")
    persisted = db.trades.insert_one.await_args[0][0]
    assert persisted["status"] == "pending"  # limit order not filled yet
    assert result.get("exchange_order_id") == "LIM456"


@pytest.mark.asyncio
async def test_engine_handles_exchange_error_gracefully():
    db = _mock_db()
    db.trades.count_documents = AsyncMock(return_value=0)

    fake_client = MagicMock()
    import ccxt
    fake_client.create_market_order = AsyncMock(side_effect=ccxt.InsufficientFunds("insufficient funds"))
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
    # A13 P0-01 — a NETWORK failure may have reached the exchange: UNKNOWN, never resent
    fake_client.create_market_order = AsyncMock(side_effect=ccxt.RequestTimeout("timeout"))
    with patch("crypto_bridge.binance_engine.get_db", return_value=db), \
         patch("crypto_bridge.binance_engine.audit_pre_trade",
               new=AsyncMock(return_value={"ok": True, "audit": [], "context": {}})), \
         patch("crypto_bridge.binance_engine.BinanceClient", return_value=fake_client):
        result2 = await BinanceCCXTEngine().execute(
            user_id="u1", account=_make_account(),
            signal=_make_signal(entry_price=60001.0), cfg_account_id="acc1",
        )
    assert result2["blocked"] == "exchange_unknown" and "NOT be resent" in result2["reason"]
    assert [i["status"] for i in db.execution_intents.rows] == ["rejected", "unknown"]


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.unit
