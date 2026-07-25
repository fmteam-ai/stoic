"""Regression: Safety Guardian server-side hard floors for LIVE accounts.

Validates that live trades are refused when ANY of these caps would be breached:
  - equity_known        — account equity must be > 0
  - equity_vs_balance   — equity/balance must be > MIN_EQUITY_VS_BALANCE_PCT
  - free_margin_floor   — free_margin/equity must be > MIN_FREE_MARGIN_PCT
  - per_trade_risk_cap  — SL-distance × lot × pip$ ≤ MAX_RISK_PCT_PER_TRADE
  - lot_vs_equity_sanity — lot exposure ≤ sanity ceiling
  - daily_loss_cap      — today's realized PnL > -MAX_DAILY_LOSS_PCT × balance
  - total_open_risk_cap — sum of open risk + new risk ≤ MAX_TOTAL_OPEN_RISK_PCT

For paper accounts, all checks bypass with mode=paper marker.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock


class _AsyncCursor:
    """Mimics a Motor cursor that supports both `async for` and to_list."""
    def __init__(self, docs):
        self._docs = list(docs)

    def __aiter__(self):
        async def gen():
            for d in self._docs:
                yield d
        return gen()

    async def to_list(self, length=None):
        return list(self._docs)


def _agg_cursor(docs):
    return _AsyncCursor(docs)


def _live_acct(equity=10000.0, balance=10000.0, free_margin=8000.0):
    return {"_id": "acct_1", "mode": "live", "broker": "MT5",
            "equity": equity, "balance": balance, "free_margin": free_margin}


def _paper_acct():
    return {"_id": "acct_2", "mode": "paper", "broker": "PAPER",
            "equity": 10000.0, "balance": 10000.0, "free_margin": 10000.0}


def _good_signal(lot=0.05):
    """Risk ~$50 on $10k equity (0.5%) — well within all caps."""
    return {"symbol": "XAUUSD", "action": "SELL", "lot_size": lot,
            "entry_price": 3950.0, "stop_loss": 3960.0, "take_profit": 3920.0}


def _empty_db():
    fake_db = MagicMock()
    # No open trades (async-for cursor), no realized PnL today (aggregate).
    fake_db.trades.find = MagicMock(return_value=_AsyncCursor([]))
    fake_db.trades.aggregate = MagicMock(return_value=_AsyncCursor([]))
    # No FRED cache → macro gate fails-open (regime="no_macro_data")
    fake_db.fred_cache.find_one = AsyncMock(return_value=None)
    return fake_db


@pytest.mark.asyncio
async def test_paper_account_always_bypasses():
    from safety_guardian import audit_pre_trade
    result = await audit_pre_trade(
        db=_empty_db(), account=_paper_acct(), signal=_good_signal(),
        user_id="u1", cfg_account_id="acct_2",
    )
    assert result["ok"] is True
    assert result["blocked_by"] is None
    names = [a["name"] for a in result["audit"]]
    assert "paper_mode_bypass" in names


@pytest.mark.asyncio
async def test_live_account_zero_equity_refused():
    from safety_guardian import audit_pre_trade
    acct = _live_acct(equity=0, balance=0)
    result = await audit_pre_trade(
        db=_empty_db(), account=acct, signal=_good_signal(),
        user_id="u1", cfg_account_id="acct_1",
    )
    assert result["ok"] is False
    assert result["blocked_by"] == "equity_known"


@pytest.mark.asyncio
async def test_live_account_deep_drawdown_refused():
    """Equity dropped to 50% of balance → refuse new trade."""
    from safety_guardian import audit_pre_trade
    acct = _live_acct(equity=5000.0, balance=10000.0, free_margin=4500.0)
    result = await audit_pre_trade(
        db=_empty_db(), account=acct, signal=_good_signal(),
        user_id="u1", cfg_account_id="acct_1",
    )
    assert result["ok"] is False
    assert result["blocked_by"] == "equity_vs_balance_floor"


@pytest.mark.asyncio
async def test_live_account_low_free_margin_refused():
    from safety_guardian import audit_pre_trade
    acct = _live_acct(equity=10000.0, balance=10000.0, free_margin=500.0)  # 5%
    result = await audit_pre_trade(
        db=_empty_db(), account=acct, signal=_good_signal(),
        user_id="u1", cfg_account_id="acct_1",
    )
    assert result["ok"] is False
    assert result["blocked_by"] == "free_margin_floor"


@pytest.mark.asyncio
async def test_live_account_oversized_trade_refused():
    """5-lot XAU on $10k equity → per-trade risk way over 3% cap."""
    from safety_guardian import audit_pre_trade
    result = await audit_pre_trade(
        db=_empty_db(), account=_live_acct(),
        signal=_good_signal(lot=5.0),
        user_id="u1", cfg_account_id="acct_1",
    )
    assert result["ok"] is False
    assert result["blocked_by"] in ("per_trade_risk_cap", "lot_vs_equity_sanity")


@pytest.mark.asyncio
async def test_live_account_good_trade_passes():
    from safety_guardian import audit_pre_trade
    result = await audit_pre_trade(
        db=_empty_db(), account=_live_acct(),
        signal=_good_signal(lot=0.05),
        user_id="u1", cfg_account_id="acct_1",
    )
    assert result["ok"] is True
    assert result["blocked_by"] is None
    # Every guard should have ok=True
    failed = [a for a in result["audit"] if not a["ok"]]
    assert failed == [], f"Unexpected failed audits: {failed}"


@pytest.mark.asyncio
async def test_live_account_daily_loss_cap_refuses():
    """Already lost 7% of balance today → refuse another trade (cap 6%)."""
    from safety_guardian import audit_pre_trade
    fake_db = MagicMock()
    # Realized -700 USD today via the aggregation path; no open trades.
    fake_db.trades.aggregate = MagicMock(return_value=_AsyncCursor(
        [{"_id": None, "pnl": -700.0}]))
    fake_db.trades.find = MagicMock(return_value=_AsyncCursor([]))
    fake_db.fred_cache.find_one = AsyncMock(return_value=None)
    result = await audit_pre_trade(
        db=fake_db, account=_live_acct(),
        signal=_good_signal(lot=0.05),
        user_id="u1", cfg_account_id="acct_1",
    )
    assert result["ok"] is False
    assert result["blocked_by"] == "daily_loss_cap"


@pytest.mark.asyncio
async def test_live_account_aggregate_risk_cap_refuses(monkeypatch):
    """Open trades already chew 9% of equity, new trade would push past 9% cap."""
    from safety_guardian import audit_pre_trade
    fake_db = MagicMock()
    # No realized PnL today (aggregate empty); big open trades chew equity.
    fake_db.trades.aggregate = MagicMock(return_value=_AsyncCursor([]))
    fake_db.trades.find = MagicMock(return_value=_AsyncCursor([
        {"symbol": "XAUUSD", "lot_size": 1.0, "entry_price": 3950.0,
         "stop_loss": 3960.0},
    ]))
    result = await audit_pre_trade(
        db=fake_db, account=_live_acct(),
        signal=_good_signal(lot=0.05),
        user_id="u1", cfg_account_id="acct_1",
    )
    assert result["ok"] is False
    assert result["blocked_by"] == "total_open_risk_cap"


@pytest.mark.asyncio
async def test_guardian_config_exposes_thresholds():
    from safety_guardian import get_guardian_config
    cfg = get_guardian_config()
    for key in ("max_risk_pct_per_trade", "max_lot_pct_of_equity",
                "max_total_open_risk_pct", "max_daily_loss_pct",
                "min_free_margin_pct", "min_equity_vs_balance_pct"):
        assert key in cfg
        assert cfg[key] > 0


@pytest.mark.asyncio
async def test_mt5_engine_calls_guardian_and_blocks_on_failure(monkeypatch):
    """MT5BridgeEngine.execute() must invoke the guardian and bail on safety failure."""
    from execution import MT5BridgeEngine

    fake_db = MagicMock()
    fake_db.trades.count_documents = AsyncMock(return_value=0)
    fake_db.trades.insert_one = AsyncMock()
    fake_db.safety_blocks.insert_one = AsyncMock()
    monkeypatch.setattr("execution.get_db", lambda: fake_db)
    async def _gate_open(*a, **k):  # deterministic: bypass entitlement+identity gates
        return None
    monkeypatch.setattr("entitlements.verify_execution_entitlement", _gate_open)
    monkeypatch.setattr("vps_agent.verify_execution_identity", _gate_open)
    monkeypatch.setattr("microstructure.is_market_closed", lambda s: None)


    async def fake_audit(**kw):  # noqa: ARG001
        return {"ok": False, "blocked_by": "per_trade_risk_cap",
                "audit": [{"name": "per_trade_risk_cap", "ok": False,
                           "reason": "test", "value": 999}],
                "evaluated_at": "2026-01-01T00:00:00+00:00",
                "context": {}}
    monkeypatch.setattr("execution.audit_pre_trade", fake_audit)
    monkeypatch.setattr("execution.get_quote", AsyncMock(return_value={}))

    engine = MT5BridgeEngine()
    result = await engine.execute(
        user_id="u1", account=_live_acct(),
        signal={**_good_signal(), "lot_size": 0.1},
        max_concurrent=5, cfg_account_id="acct_1",
    )
    assert result.get("blocked") == "safety_guardian"
    assert result.get("safety_blocked_by") == "per_trade_risk_cap"
    fake_db.trades.insert_one.assert_not_called()


@pytest.mark.asyncio
async def test_mt5_engine_stamps_safety_audit_on_good_trade(monkeypatch):
    """When guardian passes, the inserted trade doc gets a `safety_audit` field."""
    from execution import MT5BridgeEngine

    fake_db = MagicMock()
    fake_db.trades.count_documents = AsyncMock(return_value=0)
    inserted = MagicMock()
    inserted.inserted_id = "tid"
    fake_db.trades.insert_one = AsyncMock(return_value=inserted)
    monkeypatch.setattr("execution.get_db", lambda: fake_db)
    async def _gate_open(*a, **k):  # deterministic: bypass entitlement+identity gates
        return None
    monkeypatch.setattr("entitlements.verify_execution_entitlement", _gate_open)
    monkeypatch.setattr("vps_agent.verify_execution_identity", _gate_open)
    monkeypatch.setattr("microstructure.is_market_closed", lambda s: None)

    monkeypatch.setattr("execution.ws_manager.broadcast", AsyncMock())

    fake_audit = {"ok": True, "blocked_by": None,
                  "audit": [{"name": "per_trade_risk_cap", "ok": True}],
                  "evaluated_at": "2026-01-01T00:00:00+00:00",
                  "context": {"equity": 10000.0}}

    async def passing_audit(**kw):  # noqa: ARG001
        return fake_audit
    monkeypatch.setattr("execution.audit_pre_trade", passing_audit)
    monkeypatch.setattr("execution.get_quote", AsyncMock(return_value={}))

    engine = MT5BridgeEngine()
    result = await engine.execute(
        user_id="u1", account=_live_acct(),
        signal=_good_signal(lot=0.05),
        max_concurrent=5, cfg_account_id="acct_1",
    )
    assert "blocked" not in result
    inserted_doc = fake_db.trades.insert_one.await_args.args[0]
    assert inserted_doc.get("safety_audit") == fake_audit
