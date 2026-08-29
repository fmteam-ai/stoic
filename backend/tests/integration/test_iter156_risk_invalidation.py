"""iter-156 — the two RC-freeze P0 corrections, proven:

P0-A  Nitro slippage evidence: the trusted-slippage window (50) now exceeds
      the Nitro minimum (20) — with 20+ trusted samples the evidence gate
      CAN pass, and the telemetry carries the full distribution
      (median/p75/p90/p95/max/age).

P0-B  Risk-state content fingerprint: mutations that keep document counts
      constant invalidate the maintained state IMMEDIATELY —
        1. open position lot change
        2. open position risk_pct change
        3. partial fill (lot reduction)
        4. manual broker position change surfaced by Position Truth
        5. closed-trade P&L correction → daily/weekly loss refreshed
"""
import asyncio
import os
import uuid
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.integration

DB_NAME = f"stoic_test_rs_{uuid.uuid4().hex[:8]}"


def _now():
    return datetime.now(timezone.utc).isoformat()


def _fresh_db():
    os.environ["DB_NAME"] = DB_NAME
    import database
    database._client = None
    from database import get_db
    return get_db()


def _account(bal=10000.0):
    return {"_id": "rs_acct", "balance": bal,
            "current_spreads": {}, "spreads_updated_at": None}


async def _tele(db):
    from modules.pamm.strategy_guard import _telemetry
    return await _telemetry(db, {}, _account(),
                            {"symbol": "XAUUSD", "side": "BUY"})


async def _seed(db):
    await db.trades.insert_one(
        {"_id": "open1", "account_id": "rs_acct", "status": "open",
         "symbol": "XAUUSD", "action": "BUY", "lot_size": 1.0,
         "risk_pct": 1.0, "created_at": _now()})
    await db.trades.insert_one(
        {"_id": "closed1", "account_id": "rs_acct", "status": "closed",
         "symbol": "XAUUSD", "pnl": -100.0, "closed_at": _now(),
         "created_at": _now()})


async def _invalidation_scenarios():
    db = _fresh_db()
    try:
        await _seed(db)
        t = await _tele(db)
        assert t["risk_state_source"] == "recomputed"
        assert t["symbol_open_lots"] == 1.0
        # second read serves the maintained state
        t = await _tele(db)
        assert t["risk_state_source"] == "maintained"

        # 1. open position LOT change (count unchanged) → invalid NOW
        await db.trades.update_one({"_id": "open1"},
                                   {"$set": {"lot_size": 2.5}})
        t = await _tele(db)
        assert t["risk_state_source"] == "recomputed"
        assert t["symbol_open_lots"] == 2.5
        assert (await _tele(db))["risk_state_source"] == "maintained"

        # 2. open position RISK_PCT change → invalid NOW
        await db.trades.update_one({"_id": "open1"},
                                   {"$set": {"risk_pct": 3.0}})
        t = await _tele(db)
        assert t["risk_state_source"] == "recomputed"
        assert t["open_risk_pct_sum"] == 3.0
        assert (await _tele(db))["risk_state_source"] == "maintained"

        # 3. PARTIAL FILL (lot reduced in place) → invalid NOW
        await db.trades.update_one({"_id": "open1"},
                                   {"$set": {"lot_size": 1.2}})
        t = await _tele(db)
        assert t["risk_state_source"] == "recomputed"
        assert t["symbol_open_lots"] == 1.2

        # 4. manual broker position change → Position Truth reconciler
        # rewrites the trade's size → Risk State invalid NOW
        assert (await _tele(db))["risk_state_source"] == "maintained"
        await db.trades.update_one(
            {"_id": "open1"},
            {"$set": {"lot_size": 0.7,
                      "reconciled_from": "broker_position_truth"}})
        t = await _tele(db)
        assert t["risk_state_source"] == "recomputed"
        assert t["symbol_open_lots"] == 0.7

        # 5. closed-trade P&L CORRECTION → weekly/daily loss refreshed NOW
        assert (await _tele(db))["risk_state_source"] == "maintained"
        assert (await _tele(db))["weekly_loss_pct"] == 1.0   # -100/10000
        await db.trades.update_one({"_id": "closed1"},
                                   {"$set": {"pnl": -500.0}})
        t = await _tele(db)
        assert t["risk_state_source"] == "recomputed"
        assert t["weekly_loss_pct"] == 5.0
        assert t["daily_loss_pct"] == 5.0
    finally:
        await db.client.drop_database(DB_NAME)


def test_risk_state_content_fingerprint_invalidation():
    asyncio.run(_invalidation_scenarios())


async def _nitro_evidence_scenario():
    db = _fresh_db()
    from modules.pamm.strategy_guard import (MIN_SLIPPAGE_SAMPLES,
                                             SLIPPAGE_EVIDENCE_WINDOW,
                                             slippage_evidence_violation)
    try:
        # the window MUST exceed every eligibility minimum (the P0-A bug
        # was window=10 < Nitro's 20)
        assert SLIPPAGE_EVIDENCE_WINDOW >= max(MIN_SLIPPAGE_SAMPLES.values())

        # 25 trusted measured fills (> Nitro minimum of 20)
        await db.trades.insert_many([
            {"account_id": "rs_acct", "status": "closed",
             "slippage_checked": True, "slippage_pips": float(i % 7) / 10,
             "pamm_slippage_verified": True,
             "created_at": _now(), "closed_at": _now(), "pnl": 1.0}
            for i in range(25)])
        t = await _tele(db)
        assert t["recent_slippage_sample_count"] == 25
        # Nitro (MAXIMUM=20) gate CAN pass through this path now
        assert slippage_evidence_violation("MAXIMUM", t) is None
        assert slippage_evidence_violation("VERY_HIGH", t) is None
        # full distribution + evidence age exposed
        for k in ("recent_slippage_pips", "recent_slippage_p75_pips",
                  "recent_slippage_p90_pips", "recent_slippage_p95_pips",
                  "recent_slippage_max_pips"):
            assert t[k] is not None
        assert t["slippage_evidence_age_s"] is not None
        # cached read carries the identical evidence
        t2 = await _tele(db)
        assert t2["risk_state_source"] == "maintained"
        assert t2["recent_slippage_sample_count"] == 25
        assert t2["recent_slippage_p90_pips"] == t["recent_slippage_p90_pips"]
        assert slippage_evidence_violation("MAXIMUM", t2) is None

        # below-minimum still blocks (eligibility floor unchanged)
        assert slippage_evidence_violation(
            "MAXIMUM", {"recent_slippage_sample_count": 19}) is not None
        assert slippage_evidence_violation(
            "VERY_HIGH", {"recent_slippage_sample_count": 9}) is not None
    finally:
        await db.client.drop_database(DB_NAME)


def test_nitro_slippage_evidence_gate_can_pass():
    asyncio.run(_nitro_evidence_scenario())
