"""Tests for the FRED-driven macro-regime hard gate (XAUUSD only).

All tests mock the fred_cache reads via monkeypatching `database.get_db`.
"""
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock


def _fake_db_with_fred(*, dgs10_wow=0.0, dxy_latest=120.0, dxy_wow=0.0):
    """Build a MagicMock db whose `fred_cache.find_one({_id: X})` returns the
    seeded row for DGS10/DTWEXBGS, else None."""
    now = datetime.now(timezone.utc).isoformat()
    rows = {
        "DGS10": {"_id": "DGS10", "series_id": "DGS10", "latest": 4.20,
                  "wow_delta": dgs10_wow, "dod_delta": 0.0, "unit": "%",
                  "date": "2026-02-01", "fetched_at": now},
        "DTWEXBGS": {"_id": "DTWEXBGS", "series_id": "DTWEXBGS",
                     "latest": dxy_latest, "wow_delta": dxy_wow,
                     "dod_delta": 0.0, "unit": "", "date": "2026-02-01",
                     "fetched_at": now},
    }

    async def _find_one(q):
        return rows.get((q or {}).get("_id"))

    db = MagicMock()
    db.fred_cache.find_one = AsyncMock(side_effect=_find_one)
    # safety_guardian integration also needs trades.find/delete
    db.trades.find = MagicMock(return_value=MagicMock(
        to_list=AsyncMock(return_value=[]),
    ))
    return db


def _fake_db_empty_fred():
    db = MagicMock()
    db.fred_cache.find_one = AsyncMock(return_value=None)
    db.trades.find = MagicMock(return_value=MagicMock(
        to_list=AsyncMock(return_value=[]),
    ))
    return db


# ---------- pass-through cases ----------
@pytest.mark.asyncio
async def test_non_gated_symbol_passes(monkeypatch):
    import macro_gate
    monkeypatch.setattr("macro_gate.get_db",
                        lambda: _fake_db_with_fred(dgs10_wow=0.30))
    res = await macro_gate.evaluate("BTCUSD", "BUY")
    assert res["ok"] is True
    assert res["regime"] == "symbol_not_gated"


@pytest.mark.asyncio
async def test_hold_action_passes(monkeypatch):
    import macro_gate
    monkeypatch.setattr("macro_gate.get_db",
                        lambda: _fake_db_with_fred(dgs10_wow=0.50))
    res = await macro_gate.evaluate("XAUUSD", "HOLD")
    assert res["ok"] is True
    assert res["regime"] == "non_directional_action"


@pytest.mark.asyncio
async def test_no_cache_fails_open(monkeypatch):
    import macro_gate
    monkeypatch.setattr("macro_gate.get_db", _fake_db_empty_fred)
    res = await macro_gate.evaluate("XAUUSD", "BUY")
    assert res["ok"] is True
    assert res["regime"] == "no_macro_data"


# ---------- block cases ----------
@pytest.mark.asyncio
async def test_yield_surge_blocks_buy(monkeypatch):
    import macro_gate
    monkeypatch.setattr("macro_gate.get_db",
                        lambda: _fake_db_with_fred(dgs10_wow=0.30))
    res = await macro_gate.evaluate("XAUUSD", "BUY")
    assert res["ok"] is False
    assert res["blocked_by"] == "real_yield_surge"


@pytest.mark.asyncio
async def test_yield_surge_allows_sell(monkeypatch):
    import macro_gate
    monkeypatch.setattr("macro_gate.get_db",
                        lambda: _fake_db_with_fred(dgs10_wow=0.30))
    res = await macro_gate.evaluate("XAUUSD", "SELL")
    assert res["ok"] is True


@pytest.mark.asyncio
async def test_yield_collapse_blocks_sell(monkeypatch):
    import macro_gate
    monkeypatch.setattr("macro_gate.get_db",
                        lambda: _fake_db_with_fred(dgs10_wow=-0.30))
    res = await macro_gate.evaluate("XAUUSD", "SELL")
    assert res["ok"] is False
    assert res["blocked_by"] == "real_yield_collapse"


@pytest.mark.asyncio
async def test_dxy_surge_blocks_buy(monkeypatch):
    """+2% on 120 latest → wow_delta=2.4 (raw delta) → pct = 2.0%."""
    import macro_gate
    monkeypatch.setattr("macro_gate.get_db",
                        lambda: _fake_db_with_fred(dxy_latest=120.0, dxy_wow=2.4))
    res = await macro_gate.evaluate("XAUUSD", "BUY")
    assert res["ok"] is False
    assert res["blocked_by"] == "usd_surge"


@pytest.mark.asyncio
async def test_dxy_collapse_blocks_sell(monkeypatch):
    import macro_gate
    monkeypatch.setattr("macro_gate.get_db",
                        lambda: _fake_db_with_fred(dxy_latest=120.0, dxy_wow=-2.4))
    res = await macro_gate.evaluate("XAUUSD", "SELL")
    assert res["ok"] is False
    assert res["blocked_by"] == "usd_collapse"


@pytest.mark.asyncio
async def test_neutral_macro_allows_both(monkeypatch):
    import macro_gate
    monkeypatch.setattr("macro_gate.get_db",
                        lambda: _fake_db_with_fred(dgs10_wow=0.05,
                                                   dxy_latest=120.0, dxy_wow=0.5))
    buy = await macro_gate.evaluate("XAUUSD", "BUY")
    sell = await macro_gate.evaluate("XAUUSD", "SELL")
    assert buy["ok"] is True
    assert sell["ok"] is True
    assert buy["regime"] == "macro_neutral"


# ---------- status endpoint ----------
@pytest.mark.asyncio
async def test_gate_status_blocks_buy_only_on_yield_surge(monkeypatch):
    import macro_gate
    monkeypatch.setattr("macro_gate.get_db",
                        lambda: _fake_db_with_fred(dgs10_wow=0.30))
    status = await macro_gate.gate_status()
    assert status["symbol"] == "XAUUSD"
    assert status["buy_ok"] is False
    assert status["sell_ok"] is True
    assert len(status["blocked"]) == 1
    assert status["blocked"][0]["action"] == "BUY"


@pytest.mark.asyncio
async def test_gate_status_neutral(monkeypatch):
    import macro_gate
    monkeypatch.setattr("macro_gate.get_db",
                        lambda: _fake_db_with_fred(dgs10_wow=0.0, dxy_wow=0.0))
    status = await macro_gate.gate_status()
    assert status["buy_ok"] is True
    assert status["sell_ok"] is True
    assert status["regime"] == "macro_neutral"
    assert status["blocked"] == []


# ---------- integration with safety_guardian ----------
@pytest.mark.asyncio
async def test_safety_guardian_blocks_buy_on_yield_surge(monkeypatch):
    """Full SG run with yield surge → blocked_by macro_regime_gate."""
    import macro_gate
    from safety_guardian import audit_pre_trade
    fake_db = _fake_db_with_fred(dgs10_wow=0.30)
    monkeypatch.setattr("macro_gate.get_db", lambda: fake_db)
    account = {
        "_id": "acct1", "mode": "live", "balance": 10_000, "equity": 10_000,
        "free_margin": 8_000, "account_type": "standard",
    }
    signal = {
        "symbol": "XAUUSD", "action": "BUY",
        "lot_size": 0.01, "entry_price": 2050.0, "stop_loss": 2040.0,
        "take_profit": 2070.0,
    }
    result = await audit_pre_trade(
        db=fake_db, account=account, signal=signal,
        user_id="u1", cfg_account_id="acct1",
    )
    assert result["ok"] is False
    assert result["blocked_by"] == "macro_regime_gate"


@pytest.mark.asyncio
async def test_safety_guardian_allows_when_macro_disabled(monkeypatch):
    import macro_gate
    from safety_guardian import audit_pre_trade
    fake_db = _fake_db_with_fred(dgs10_wow=0.30)
    monkeypatch.setattr("macro_gate.get_db", lambda: fake_db)
    monkeypatch.setattr(macro_gate, "ENABLED", False)
    account = {
        "_id": "acct1", "mode": "live", "balance": 10_000, "equity": 10_000,
        "free_margin": 8_000, "account_type": "standard",
    }
    signal = {
        "symbol": "XAUUSD", "action": "BUY",
        "lot_size": 0.01, "entry_price": 2050.0, "stop_loss": 2040.0,
        "take_profit": 2070.0,
    }
    result = await audit_pre_trade(
        db=fake_db, account=account, signal=signal,
        user_id="u1", cfg_account_id="acct1",
    )
    # Macro gate is disabled → blocked_by must NOT be macro_regime_gate
    assert result.get("blocked_by") != "macro_regime_gate"


@pytest.mark.asyncio
async def test_safety_guardian_passes_btc_during_yield_surge(monkeypatch):
    """BTC is not gated by macro — sails through the macro check."""
    import macro_gate
    from safety_guardian import audit_pre_trade
    fake_db = _fake_db_with_fred(dgs10_wow=0.30)
    monkeypatch.setattr("macro_gate.get_db", lambda: fake_db)
    account = {
        "_id": "acct1", "mode": "live", "balance": 10_000, "equity": 10_000,
        "free_margin": 8_000, "account_type": "standard",
    }
    signal = {
        "symbol": "BTCUSD", "action": "BUY",
        "lot_size": 0.01, "entry_price": 60_000.0, "stop_loss": 59_000.0,
        "take_profit": 62_000.0,
    }
    result = await audit_pre_trade(
        db=fake_db, account=account, signal=signal,
        user_id="u1", cfg_account_id="acct1",
    )
    assert result.get("blocked_by") != "macro_regime_gate"
