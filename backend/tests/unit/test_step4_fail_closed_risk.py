"""Roadmap step 4 — fail-closed risk checks (C2, H6, H7)."""
import pytest
from unittest.mock import AsyncMock, MagicMock

from safety_guardian import sl_tp_side_violation
from risk_engine import risk_engine_evaluate

pytestmark = pytest.mark.unit


# ------------------------------------------------------------- H6/H7 SL/TP side
@pytest.mark.parametrize("sig", [
    {"action": "BUY", "entry_price": 100.0, "stop_loss": 99.0, "take_profit": 102.0},
    {"action": "SELL", "entry_price": 100.0, "stop_loss": 101.0, "take_profit": 98.0},
    {"action": "BUY", "entry_price": 100.0, "stop_loss": 99.0, "take_profit": 0},        # SL-only ok
    {"action": "BUY", "entry_price": 100.0, "stop_loss": 99.0, "take_profit": None},
    {"action": "HOLD", "entry_price": 100.0, "stop_loss": 101.0, "take_profit": 99.0},   # not an order
])
def test_h6_correct_sides_pass(sig):
    assert sl_tp_side_violation(sig) is None


@pytest.mark.parametrize("sig,needle", [
    ({"action": "BUY", "entry_price": 100.0, "stop_loss": 101.0, "take_profit": 102.0}, "stop-loss"),
    ({"action": "BUY", "entry_price": 100.0, "stop_loss": 100.0, "take_profit": 102.0}, "stop-loss"),   # SL == entry
    ({"action": "BUY", "entry_price": 100.0, "stop_loss": 99.0, "take_profit": 98.0}, "take-profit"),
    ({"action": "SELL", "entry_price": 100.0, "stop_loss": 99.0, "take_profit": 98.0}, "stop-loss"),
    ({"action": "SELL", "entry_price": 100.0, "stop_loss": 101.0, "take_profit": 103.0}, "take-profit"),
    ({"action": "SELL", "entry_price": 100.0, "stop_loss": "abc", "take_profit": 98.0}, "numeric"),
])
def test_h7_wrong_sides_are_named(sig, needle):
    reason = sl_tp_side_violation(sig)
    assert reason and needle in reason


def _db():
    db = MagicMock()
    db.trades.find = MagicMock(return_value=MagicMock(to_list=AsyncMock(return_value=[])))
    db.trades.aggregate = MagicMock(return_value=_cursor([]))
    db.fred_cache.find_one = AsyncMock(return_value=None)
    db.intraday_candles.find_one = AsyncMock(return_value=None)
    db.execution_intents.find_one = AsyncMock(return_value=None)
    return db


class _cursor:
    def __init__(self, items):
        self._it = iter(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


@pytest.mark.asyncio
async def test_h7_guardian_blocks_wrong_side_even_for_paper():
    from safety_guardian import audit_pre_trade
    paper = {"_id": "a", "mode": "paper", "equity": 10_000, "balance": 10_000}
    bad = {"symbol": "XAUUSD", "action": "BUY", "lot_size": 0.05,
           "entry_price": 3950.0, "stop_loss": 3960.0, "take_profit": 3990.0}
    r = await audit_pre_trade(db=_db(), account=paper, signal=bad, user_id="u", cfg_account_id="a")
    assert r["ok"] is False and r["blocked_by"] == "sl_tp_side"
    good = dict(bad, stop_loss=3940.0)
    r = await audit_pre_trade(db=_db(), account=paper, signal=good, user_id="u", cfg_account_id="a")
    assert r["ok"] is True and [a["name"] for a in r["audit"]][0] == "sl_tp_side"


# ------------------------------------------------------------- C2 fail-closed
@pytest.mark.asyncio
async def test_c2_cvar_error_blocks_instead_of_skipping(monkeypatch):
    import risk_engine
    async def boom(*a, **k):
        raise RuntimeError("cvar backend down")
    monkeypatch.setattr(risk_engine, "cvar_budget_check", boom)
    async def pnls(*a, **k):
        return 0.0, 0.0, 0.0
    monkeypatch.setattr(risk_engine, "_period_pnls", pnls)
    db = _db()
    acct = {"equity": 10_000, "balance": 10_000}
    sig = {"symbol": "XAUUSD", "entry_price": 4000.0, "stop_loss": 3990.0}
    out = await risk_engine_evaluate(db, "u", {}, acct, sig, 0.01, "acct")
    assert out["allow"] is False
    assert out["blocked_by"]["name"] == "cvar_budget" and "fail-closed" in out["blocked_by"]["detail"]


def test_c2_bot_runner_risk_engine_is_fail_closed():
    import inspect
    import bot_runner
    src = inspect.getsource(bot_runner)
    assert "risk engine failed (fail-open)" not in src
    assert "risk_engine_error_block" in src and "FAIL-CLOSED" in src
