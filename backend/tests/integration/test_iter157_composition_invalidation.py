"""iter-157 — the reviewer's composition edge case, closed and proven.

Sum-based fingerprints were blind to a position changing WHAT it is while
keeping the totals equal. The adversarial cases below all keep
open_count / total lots / total risk IDENTICAL and MUST still invalidate
the maintained risk state immediately:

    1. BUY XAUUSD 1.0 → SELL XAUUSD 1.0     (side flip)
    2. XAUUSD → EURUSD, same lots/risk       (symbol swap)
    3. stop-loss change only                 (risk-affecting field)
    4. BUY XAUUSD → SELL EURUSD              (the reviewer's exact case)
    5. closed_at moved into today, same pnl  (daily loss composition)
"""
import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.integration

DB_NAME = f"stoic_test_comp_{uuid.uuid4().hex[:8]}"


def _now():
    return datetime.now(timezone.utc).isoformat()


def _fresh_db():
    os.environ["DB_NAME"] = DB_NAME
    import database
    database._client = None
    from database import get_db
    return get_db()


async def _tele(db, symbol="XAUUSD"):
    from modules.pamm.strategy_guard import _telemetry
    account = {"_id": "comp_acct", "balance": 10000.0,
               "current_spreads": {}, "spreads_updated_at": None}
    return await _telemetry(db, {}, account,
                            {"symbol": symbol, "side": "BUY"})


async def _assert_invalidates(db, mutation: dict, check):
    """Cache must be warm, then the mutation must force a recompute."""
    t = await _tele(db)
    assert t["risk_state_source"] == "maintained", "cache should be warm"
    await db.trades.update_one({"_id": "pos1"}, {"$set": mutation})
    t = await _tele(db)
    assert t["risk_state_source"] == "recomputed", \
        f"mutation {mutation} did NOT invalidate the maintained state"
    check(t)


async def _scenarios():
    db = _fresh_db()
    try:
        await db.trades.insert_one(
            {"_id": "pos1", "account_id": "comp_acct", "status": "open",
             "symbol": "XAUUSD", "action": "BUY", "lot_size": 1.0,
             "risk_pct": 1.0, "stop_loss": 1900.0, "created_at": _now()})
        t = await _tele(db)
        assert t["risk_state_source"] == "recomputed"
        assert t["symbol_open_lots"] == 1.0

        # 1. side flip — identical count/lots/risk
        await _assert_invalidates(
            db, {"action": "SELL"},
            lambda t: None)

        # 2. symbol swap — identical count/lots/risk
        await _assert_invalidates(
            db, {"symbol": "EURUSD"},
            lambda t: t)
        t = await _tele(db)          # XAUUSD lots must now be zero
        assert t["symbol_open_lots"] == 0.0
        assert (await _tele(db, symbol="EURUSD"))["symbol_open_lots"] == 1.0

        # 3. stop-loss change only
        await _assert_invalidates(
            db, {"stop_loss": 1850.0},
            lambda t: None)

        # 4. the reviewer's exact case in one write:
        #    SELL EURUSD 1.0/1% → BUY XAUUSD 1.0/1%
        await _assert_invalidates(
            db, {"symbol": "XAUUSD", "action": "BUY"},
            lambda t: None)
        assert (await _tele(db))["symbol_open_lots"] == 1.0

        # 5. daily-loss composition: same WEEK totals, closed_at moved
        #    from 3 days ago into today → daily loss must refresh NOW
        old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        await db.trades.insert_one(
            {"_id": "closed1", "account_id": "comp_acct",
             "status": "closed", "pnl": -300.0, "closed_at": old,
             "created_at": old})
        t = await _tele(db)
        assert t["risk_state_source"] == "recomputed"
        # (weekly counts it; daily may not, depending on week boundaries)
        weekly_before = t["weekly_loss_pct"]
        assert (await _tele(db))["risk_state_source"] == "maintained"
        await db.trades.update_one({"_id": "closed1"},
                                   {"$set": {"closed_at": _now()}})
        t = await _tele(db)
        assert t["risk_state_source"] == "recomputed"
        assert t["weekly_loss_pct"] == weekly_before   # same week total
        assert t["daily_loss_pct"] == 3.0              # -300/10000 today
    finally:
        await db.client.drop_database(DB_NAME)


def test_composition_changes_invalidate_risk_state():
    asyncio.run(_scenarios())


def test_open_fingerprint_is_composition_sensitive():
    """Pure fingerprint check — same totals, different composition ⇒
    different identity; identical content ⇒ identical identity."""
    from modules.pamm.risk_state import RISK_COMPOSITION_FIELDS, _norm
    assert "symbol" in RISK_COMPOSITION_FIELDS
    assert "action" in RISK_COMPOSITION_FIELDS
    assert "stop_loss" in RISK_COMPOSITION_FIELDS
    assert _norm(1.0000000001) == _norm(1.0)      # float noise tolerated
    assert _norm("XAUUSD") != _norm("EURUSD")
