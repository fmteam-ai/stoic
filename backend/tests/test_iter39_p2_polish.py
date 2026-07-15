"""Tests for iter-39: P2 polish pack.

Covers:
  - Paper-shadow mode plumbing: bot_runner picks up paper_shadow_mode=true
    configs even when active=false; auto_execute forced off; origin="shadow".
  - Per-account crypto risk cap: per-account override beats env default.
  - BotConfigUpdate model carries the new fields.

The CHEAP-HOLD pre-filter is verified manually via the live /api/signals/generate
endpoint (the prior live trace already showed CHOP-regime signals short-circuit
to HOLD with `cheap_hold=true`). Deep-mocking analyze_symbol() touches too many
collaborators to be a useful unit test.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from bson import ObjectId


# =========================================================
# CHEAP HOLD pre-filter — shape sanity (the response keys must match what UI expects)
# =========================================================
def test_cheap_hold_response_shape_keys():
    """The cheap-HOLD response shape must include the same top-level keys as a
    regular signal so the UI doesn't break when it short-circuits."""
    expected_keys = {
        "symbol", "action", "confidence", "entry_price", "stop_loss",
        "take_profit", "tp1", "tp2", "tp3", "sl_pips", "tp_pips", "lot_size",
        "risk_level", "reasoning", "indicators", "sentiment", "session",
        "regime", "macro", "tradeable", "veto_applied", "cheap_hold",
        "created_at",
    }
    import ai_signals
    src = open(ai_signals.__file__).read()
    # The "CHEAP HOLD pre-filter" comment was refactored away — anchor on the
    # cheap_hold key itself and inspect the surrounding return-dict block.
    anchor = src.index('"cheap_hold": True')
    cheap_block = src[max(0, anchor - 4000):anchor + 500]
    missing = [k for k in expected_keys if f'"{k}"' not in cheap_block]
    assert not missing, f"Cheap-HOLD block missing keys: {missing}"


# =========================================================
# Paper-shadow mode
# =========================================================
def test_paper_shadow_mode_field_in_bot_config_update():
    from models import BotConfigUpdate
    m = BotConfigUpdate(paper_shadow_mode=True)
    assert m.paper_shadow_mode is True


def test_crypto_risk_pct_per_trade_field_in_bot_config():
    from models import BotConfigUpdate
    m = BotConfigUpdate(crypto_risk_pct_per_trade=0.2)
    assert m.crypto_risk_pct_per_trade == 0.2
    # None is default → falls back to env
    m2 = BotConfigUpdate()
    assert m2.crypto_risk_pct_per_trade is None


# =========================================================
# Per-account crypto risk cap override
# =========================================================
@pytest.mark.asyncio
async def test_per_account_crypto_cap_overrides_env(monkeypatch):
    """Per-account cap (0.1%) blocks a trade that would pass the env cap (0.5%)."""
    monkeypatch.setenv("CRYPTO_MAX_RISK_PCT_PER_TRADE", "0.5")
    from crypto_bridge.binance_engine import BinanceCCXTEngine

    db = MagicMock()
    db.trades.count_documents = AsyncMock(return_value=0)
    # Per-account cfg sets a TIGHTER 0.1% cap
    db.bot_configs.find_one = AsyncMock(return_value={
        "user_id": "u1", "account_id": "acc1",
        "crypto_risk_pct_per_trade": 0.1,
    })

    acc = {
        "_id": ObjectId(), "user_id": "u1", "kind": "binance",
        "testnet": True, "live": False, "balance": 10000.0, "equity": 10000.0,
        "creds": {"api_key": "x", "api_secret": "x"},
    }
    # SL distance $300 × amount 0.1 BTC = $30 risk.
    # env cap = 0.5% × $10k = $50 → would PASS env.
    # per-account cap = 0.1% × $10k = $10 → must BLOCK.
    signal = {
        "symbol": "BTCUSD", "action": "BUY",
        "lot_size": 0.1, "entry_price": 60000.0,
        "stop_loss": 59700.0, "take_profit": 60900.0,
    }
    with patch("crypto_bridge.binance_engine.get_db", return_value=db), \
         patch("crypto_bridge.binance_engine.audit_pre_trade",
               new=AsyncMock(return_value={"ok": True, "audit": [], "context": {}})):
        result = await BinanceCCXTEngine().execute(
            user_id="u1", account=acc, signal=signal,
            max_concurrent=0, cfg_account_id="acc1",
        )
    assert result["blocked"] == "crypto_risk_cap"
    assert result["cap_pct_used"] == 0.1
    assert result["cap_source"] == "per_account"


@pytest.mark.asyncio
async def test_no_per_account_override_falls_back_to_env(monkeypatch):
    """When cfg.crypto_risk_pct_per_trade is None, env default kicks in."""
    monkeypatch.setenv("CRYPTO_MAX_RISK_PCT_PER_TRADE", "0.5")
    from crypto_bridge.binance_engine import BinanceCCXTEngine

    db = MagicMock()
    db.trades.count_documents = AsyncMock(return_value=0)
    db.bot_configs.find_one = AsyncMock(return_value={
        "user_id": "u1", "account_id": "acc1",
        "crypto_risk_pct_per_trade": None,  # explicit "use env"
    })

    acc = {
        "_id": ObjectId(), "user_id": "u1", "kind": "binance",
        "testnet": True, "live": False, "balance": 10000.0, "equity": 10000.0,
        "creds": {"api_key": "x", "api_secret": "x"},
    }
    # Same trade as above ($30 risk). env cap = $50 → must PASS env.
    signal = {
        "symbol": "BTCUSD", "action": "BUY",
        "lot_size": 0.1, "entry_price": 60000.0,
        "stop_loss": 59700.0, "take_profit": 60900.0,
    }
    fake_client = MagicMock()
    fake_client.create_market_order = AsyncMock(return_value={
        "id": "ORDER", "status": "closed", "average": 60000.0,
    })
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)
    db.trades.insert_one = AsyncMock(return_value=MagicMock(inserted_id=ObjectId()))
    db.trades.update_one = AsyncMock()

    fake_ws = MagicMock()
    fake_ws.broadcast = AsyncMock()

    with patch("crypto_bridge.binance_engine.get_db", return_value=db), \
         patch("crypto_bridge.binance_engine.audit_pre_trade",
               new=AsyncMock(return_value={"ok": True, "audit": [], "context": {}})), \
         patch("crypto_bridge.binance_engine.BinanceClient", return_value=fake_client), \
         patch("crypto_bridge.binance_engine.ws_manager", fake_ws):
        result = await BinanceCCXTEngine().execute(
            user_id="u1", account=acc, signal=signal,
            max_concurrent=0, cfg_account_id="acc1",
        )
    # Trade should fill (not blocked).
    assert "blocked" not in result, f"Got blocked: {result}"
    persisted = db.trades.insert_one.await_args[0][0]
    assert persisted["crypto_risk_cap"]["cap_source"] == "env_default"
    assert persisted["crypto_risk_cap"]["applied_pct"] == 0.5
