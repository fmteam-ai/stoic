"""Regression tests for iter-55 — multi-account circuit-breaker isolation.

Bug: When the user had two MT5 accounts connected (Roboforex $16,596.41 +
VT Markets $10,000), starting the bot for the VT Markets account fired a
circuit-breaker Telegram with equity=$26,596.41 (sum of BOTH) and
P&L=-$3,033.47 (sum of BOTH losses). The drawdown calc cross-pollinated
across accounts in two places:

  1. `circuit_breakers.check_and_trip` — `realised_pnl_since` didn't
     filter by account_id when the cfg was per-account.
  2. `trade_manager._check_daily_drawdown` — aggregated trades + equity
     user-wide AND `update_many`-disabled every cfg on a single trip.

Fix: Both paths now honour `cfg.account_id` for P&L sum, equity sum, and
the disable scope (single cfg, not all).
"""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from circuit_breakers import check_and_trip, realised_pnl_since, today_iso


# ============ circuit_breakers.check_and_trip ============
@pytest.mark.asyncio
async def test_realised_pnl_filters_by_account_when_provided():
    db = MagicMock()
    captured_query: dict = {}

    def fake_find(q):
        captured_query.update(q)
        cur = MagicMock()
        cur.to_list = AsyncMock(return_value=[
            {"pnl": -100}, {"pnl": -50},
        ])
        return cur
    db.trades.find = fake_find

    out = await realised_pnl_since(db, "user-1", today_iso(),
                                   account_id="acct-A")
    assert out == -150
    assert captured_query.get("account_id") == "acct-A"


@pytest.mark.asyncio
async def test_realised_pnl_no_account_filter_when_omitted():
    db = MagicMock()
    captured_query: dict = {}

    def fake_find(q):
        captured_query.update(q)
        cur = MagicMock()
        cur.to_list = AsyncMock(return_value=[])
        return cur
    db.trades.find = fake_find

    await realised_pnl_since(db, "user-1", today_iso())
    assert "account_id" not in captured_query


@pytest.mark.asyncio
async def test_check_and_trip_per_account_scopes_pnl_to_that_account():
    """VT Markets cfg should NOT see Roboforex losses.

    Setup: equity=$10k (VT Markets only), per-account cfg with 8% limit.
    Roboforex lost -$1500 today, VT Markets lost -$200 today.
    Expected: VT Markets drawdown = -200/10000 = -2% → NOT tripped.
    """
    db = MagicMock()

    # When called with account_id="vtmarkets" → return only VT Markets trades.
    def fake_find(q):
        cur = MagicMock()
        if q.get("account_id") == "vtmarkets":
            cur.to_list = AsyncMock(return_value=[{"pnl": -200}])
        else:
            cur.to_list = AsyncMock(return_value=[
                {"pnl": -1500},  # Roboforex
                {"pnl": -200},   # VT Markets
            ])
        return cur
    db.trades.find = fake_find
    db.bot_configs.update_one = AsyncMock()

    cfg = {"_id": "vt-cfg", "user_id": "user-1", "account_id": "vtmarkets",
           "daily_drawdown_pct": 8.0, "daily_drawdown_enabled": True,
           "weekly_drawdown_enabled": False}
    accounts = [{"_id": "vtmarkets", "equity": 10_000}]

    out = await check_and_trip(db, "user-1", cfg, accounts)
    assert out["tripped"] is False, f"Expected NO trip, got: {out}"
    assert out["pnl_today"] == -200, f"Expected VT-only P&L, got {out['pnl_today']}"
    assert out["equity"] == 10_000
    assert out["drawdown_pct"] == pytest.approx(-2.0)


@pytest.mark.asyncio
async def test_check_and_trip_per_account_disables_only_that_cfg():
    """When per-account cfg trips, only that cfg row is updated (not all)."""
    db = MagicMock()
    db.trades.find = MagicMock()
    db.trades.find.return_value.to_list = AsyncMock(return_value=[{"pnl": -2000}])
    captured: dict = {}

    async def fake_update_one(filter_q, update_q):
        captured["filter"] = filter_q
        captured["update"] = update_q
    db.bot_configs.update_one = AsyncMock(side_effect=fake_update_one)

    cfg = {"_id": "rf-cfg", "user_id": "user-1", "account_id": "roboforex",
           "daily_drawdown_pct": 8.0, "daily_drawdown_enabled": True,
           "weekly_drawdown_enabled": False}
    accounts = [{"_id": "roboforex", "equity": 10_000}]

    out = await check_and_trip(db, "user-1", cfg, accounts)
    assert out["tripped"] is True
    # The filter MUST include account_id so only this cfg is disabled.
    assert captured["filter"].get("account_id") == "roboforex"
    assert captured["filter"]["user_id"] == "user-1"


# ============ trade_manager._check_daily_drawdown ============
@pytest.mark.asyncio
async def test_trade_manager_check_drawdown_scopes_by_account():
    """Same bug, second code path. VT Markets cfg must not see Roboforex
    losses, and must not disable other cfgs on a trip."""
    from bson import ObjectId
    import trade_manager

    valid_oid = str(ObjectId())

    db = MagicMock()
    # Trade query: when account_id is specified, only return that account's trades.
    def fake_trade_find(q):
        cur = MagicMock()
        if q.get("account_id") == valid_oid:
            cur.to_list = AsyncMock(return_value=[{"pnl": -100}])  # VT-only
        else:
            cur.to_list = AsyncMock(return_value=[
                {"pnl": -1500},   # Roboforex
                {"pnl": -100},    # VT Markets
            ])
        return cur
    db.trades.find = fake_trade_find
    # Account query: when _id matches, return only that account.
    def fake_acct_find(q):
        cur = MagicMock()
        if q.get("_id"):
            cur.to_list = AsyncMock(return_value=[
                {"_id": q["_id"], "balance": 10_000},
            ])
        else:
            cur.to_list = AsyncMock(return_value=[
                {"balance": 16_596}, {"balance": 10_000},
            ])
        return cur
    db.accounts.find = fake_acct_find
    db.bot_configs.update_one = AsyncMock()

    with patch.object(trade_manager, "get_db", return_value=db), \
         patch.object(trade_manager, "ws_manager") as ws, \
         patch.object(trade_manager, "notify_circuit_breaker",
                      AsyncMock(return_value=None)):
        ws.broadcast = AsyncMock()
        cfg = {
            "_id": "vt-cfg", "user_id": "user-1",
            "account_id": valid_oid, "active": True,
            "daily_drawdown_pct": 8.0, "daily_drawdown_enabled": True,
        }
        await trade_manager._check_daily_drawdown(cfg)
        # No trip: VT P&L=-$100 / $10k = -1% > -8%
        db.bot_configs.update_one.assert_not_awaited()
        ws.broadcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_trade_manager_check_drawdown_only_disables_own_cfg_on_trip():
    """When a per-account cfg trips, only THAT cfg is disabled — the
    default cfg + other per-account cfgs MUST remain untouched."""
    from bson import ObjectId
    import trade_manager
    valid_oid = str(ObjectId())

    db = MagicMock()
    db.trades.find = MagicMock()
    db.trades.find.return_value.to_list = AsyncMock(return_value=[{"pnl": -2000}])
    db.accounts.find = MagicMock()
    db.accounts.find.return_value.to_list = AsyncMock(return_value=[
        {"_id": "x", "balance": 10_000},
    ])
    captured_filter: dict = {}

    async def fake_update(f, u):
        captured_filter.update(f)
    db.bot_configs.update_one = AsyncMock(side_effect=fake_update)

    with patch.object(trade_manager, "get_db", return_value=db), \
         patch.object(trade_manager, "ws_manager") as ws, \
         patch.object(trade_manager, "notify_circuit_breaker",
                      AsyncMock(return_value=None)):
        ws.broadcast = AsyncMock()
        cfg = {
            "_id": "rf-cfg", "user_id": "user-1",
            "account_id": valid_oid, "active": True,
            "daily_drawdown_pct": 8.0, "daily_drawdown_enabled": True,
        }
        await trade_manager._check_daily_drawdown(cfg)

    # Filter must target THIS cfg only (by _id), not user-wide.
    assert captured_filter.get("_id") == "rf-cfg"
    assert "user_id" not in captured_filter or captured_filter.get("user_id") == "rf-cfg"
