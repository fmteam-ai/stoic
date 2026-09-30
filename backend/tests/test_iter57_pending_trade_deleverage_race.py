"""Regression tests for iter-57 — auto-deleverage must not set
`close_requested=True` on a pending trade.

Bug: user got pulse "Executed SELL XAUUSD 0.08 lots @ 4084.80" but the
trade ended up with status=failed, mt5_ticket=0, error=retcode=10013
(INVALID_REQUEST). Root cause: 170ms after the trade was inserted
(status=pending), the auto-deleverage sweep flagged it for close. The EA
polled the trade and received contradictory "OPEN this trade" +
`close_requested=True` instructions → broker rejected with 10013.

Fix: `execute_deleveraging_actions` now:
  • status='open'    → close_requested=True (real broker position close).
  • status='pending' → status='cancelled' (remove from EA poll queue).
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from bson import ObjectId

import close_commands
from portfolio.risk_manager import execute_deleveraging_actions


def _patch_close(marked: int):
    """r25 P2-01: open positions are closed through the unified close protocol."""
    return patch.object(close_commands, "request_close",
                        AsyncMock(return_value={"command_id": "c1", "trades_marked_for_close": marked, "trade_ids": []}))


@pytest.mark.asyncio
async def test_open_trade_gets_close_requested():
    """Real broker position → set close_requested=True."""
    db = MagicMock()
    db.trades.update_one = AsyncMock()

    oid = str(ObjectId())
    with _patch_close(1) as rc:
        out = await execute_deleveraging_actions(db, user_id="user-1",
            actions=[{"kind": "close_trade", "trade_id": oid,
                      "reason": "auto_deleverage_var_breach"}])
    assert rc.await_args.args[1] == {"_id": ObjectId(oid), "user_id": "user-1"}
    assert rc.await_args.kwargs["reason"] == "auto_deleverage_var_breach"
    db.trades.update_one.assert_not_awaited()        # no pending-cancel write after a real close
    assert out["closed"] == 1
    assert out["cancelled"] == 0
    assert out["skipped"] == 0


@pytest.mark.asyncio
async def test_pending_trade_gets_cancelled_not_close_requested():
    """The actual bug — pending trade must be CANCELLED, not flagged
    close_requested (which would create the 10013 race)."""
    db = MagicMock()
    # request_close (status=open) misses. The pending update_one hits.
    pending_res = MagicMock(); pending_res.modified_count = 1
    calls: list = []

    async def fake_update(filter_q, update_q):
        calls.append((filter_q, update_q))
        return pending_res
    db.trades.update_one = AsyncMock(side_effect=fake_update)

    oid = str(ObjectId())
    with _patch_close(0):
        out = await execute_deleveraging_actions(db, user_id="user-1",
            actions=[{"kind": "close_trade", "trade_id": oid,
                      "reason": "auto_deleverage_sector_cap_commodity"}])
    assert out["closed"] == 0
    assert out["cancelled"] == 1
    assert out["skipped"] == 0

    # Critical: the pending-trade write must set status='cancelled', NOT
    # close_requested. Otherwise the EA would still receive the close flag.
    pending_call = next(c for c in calls if c[0].get("status") == "pending")
    upd = pending_call[1]["$set"]
    assert upd.get("status") == "cancelled"
    assert "close_requested" not in upd, \
        f"Pending trades must NOT receive close_requested — caused retcode 10013 bug. Got: {upd}"
    assert upd.get("close_reason", "").startswith("auto_deleverage")
    assert upd.get("error") and "cancelled" in upd["error"].lower()


@pytest.mark.asyncio
async def test_neither_open_nor_pending_skipped():
    """Closed/cancelled/failed trades are no-ops."""
    db = MagicMock()
    miss = MagicMock(); miss.modified_count = 0
    db.trades.update_one = AsyncMock(return_value=miss)

    oid = str(ObjectId())
    with _patch_close(0):
        out = await execute_deleveraging_actions(db, user_id="user-1",
            actions=[{"kind": "close_trade", "trade_id": oid, "reason": "x"}])
    assert out["closed"] == 0
    assert out["cancelled"] == 0
    assert out["skipped"] == 1


@pytest.mark.asyncio
async def test_invalid_object_id_skipped():
    db = MagicMock()
    db.trades.update_one = AsyncMock()
    out = await execute_deleveraging_actions(db, user_id="user-1",
        actions=[{"kind": "close_trade", "trade_id": "not-an-oid"}])
    assert out["skipped"] == 1
    db.trades.update_one.assert_not_awaited()


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.unit
