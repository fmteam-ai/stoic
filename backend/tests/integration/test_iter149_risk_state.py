"""iter-149 — O(1) maintained risk state: correctness against a scratch DB.

1. First guard telemetry read recomputes and stores; second read is served
   from the maintained state with IDENTICAL values.
2. Any trade insert/close changes the consistency basis → recompute with
   updated exposure (a stale cache can never be served).
3. NAV peak cache follows the append-only snapshot count.
"""
import asyncio
import os
import uuid
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.integration

DB_NAME = f"stoic_test_riskstate_{uuid.uuid4().hex[:8]}"


def _now():
    return datetime.now(timezone.utc).isoformat()


async def _scenario():
    os.environ["DB_NAME"] = DB_NAME
    import database
    database._client = None
    from database import get_db
    from modules.pamm import risk_state
    from modules.pamm.strategy_guard import _telemetry

    db = get_db()
    acct_id = "riskstate_acct_1"
    account = {"_id": acct_id, "balance": 10000.0}
    signal = {"symbol": "EURUSD", "action": "BUY", "lot_size": 0.1}
    try:
        await db.trades.insert_many([
            {"account_id": acct_id, "status": "open", "symbol": "EURUSD",
             "action": "BUY", "lot_size": 1.0, "risk_pct": 1.5},
            {"account_id": acct_id, "status": "open", "symbol": "GBPUSD",
             "action": "SELL", "lot_size": 0.5, "risk_pct": 0.5},
            {"account_id": acct_id, "status": "closed", "pnl": -100.0,
             "closed_at": _now()},
        ])
        t1 = await _telemetry(db, {}, account, signal)
        assert t1["risk_state_source"] == "recomputed"
        assert t1["open_positions"] == 2
        assert t1["symbol_open_lots"] == 1.0
        # signed netting: BUY EURUSD 1.0 → EUR +1, USD -1;
        # SELL GBPUSD 0.5 → GBP -0.5, USD +0.5 ⇒ USD net -0.5
        assert t1["factor_lots"] == {"EUR": 1.0, "USD": -0.5, "GBP": -0.5}
        assert t1["open_risk_pct_sum"] == 2.0
        assert t1["weekly_loss_pct"] == 1.0
        assert t1["daily_loss_pct"] == 1.0
        assert t1["consecutive_losses"] == 1

        t2 = await _telemetry(db, {}, account, signal)
        assert t2["risk_state_source"] == "maintained"
        for k in ("open_positions", "symbol_open_lots", "factor_lots",
                  "open_risk_pct_sum", "weekly_loss_pct", "daily_loss_pct",
                  "consecutive_losses"):
            assert t2[k] == t1[k], f"maintained {k} diverged"

        # (2) a new open trade changes the basis → immediate recompute
        await db.trades.insert_one(
            {"account_id": acct_id, "status": "open", "symbol": "EURUSD",
             "action": "BUY", "lot_size": 0.3, "risk_pct": 0.2})
        t3 = await _telemetry(db, {}, account, signal)
        assert t3["risk_state_source"] == "recomputed"
        assert t3["open_positions"] == 3
        assert t3["symbol_open_lots"] == 1.3
        # a close changes the week-closed count → recompute too
        await db.trades.insert_one(
            {"account_id": acct_id, "status": "closed", "pnl": 50.0,
             "closed_at": _now()})
        t4 = await _telemetry(db, {}, account, signal)
        assert t4["risk_state_source"] == "recomputed"
        assert t4["weekly_loss_pct"] == 0.5  # -100 + 50 = -50 on 10k
        assert t4["consecutive_losses"] == 0

        # (3) NAV peak cache follows the snapshot count
        pid = "riskstate_pgm_1"
        await db.pamm_nav_snapshots.insert_many([
            {"program_id": pid, "nav": 100.0, "at": _now()},
            {"program_id": pid, "nav": 120.0, "at": _now()}])
        assert await risk_state.nav_peak(db, pid) == 120.0
        cached = await db.pamm_nav_peak.find_one({"program_id": pid})
        assert cached["snap_count"] == 2
        await db.pamm_nav_snapshots.insert_one(
            {"program_id": pid, "nav": 150.0, "at": _now()})
        assert await risk_state.nav_peak(db, pid) == 150.0
    finally:
        await db.client.drop_database(DB_NAME)
        database._client = None
        risk_state._indexes_ready = False


def test_risk_state_o1_lifecycle():
    asyncio.new_event_loop().run_until_complete(_scenario())
