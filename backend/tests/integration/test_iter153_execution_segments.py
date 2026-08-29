"""iter-153 — segmented execution-quality evidence.

1. trading_session(): pure UTC session boundaries.
2. record_fill(): every measured fill lands in db.execution_quality with
   the full segmentation key (broker_server / symbol / session).
3. segments(): per-segment distribution (median/p95/worst) + calibration
   readiness against MIN_SLIPPAGE_SAMPLES (true-slippage fills only).
"""
import asyncio
import os
import uuid
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.integration

DB_NAME = f"stoic_test_seg_{uuid.uuid4().hex[:8]}"


def _dt(hour):
    return datetime(2026, 6, 15, hour, 30, tzinfo=timezone.utc)


def test_trading_session_boundaries():
    from execution_segments import trading_session
    assert trading_session(_dt(3)) == "asia"
    assert trading_session(_dt(6)) == "asia"
    assert trading_session(_dt(7)) == "london"
    assert trading_session(_dt(11)) == "london"
    assert trading_session(_dt(12)) == "overlap"
    assert trading_session(_dt(15)) == "overlap"
    assert trading_session(_dt(16)) == "newyork"
    assert trading_session(_dt(21)) == "newyork"
    assert trading_session(_dt(22)) == "asia"
    assert trading_session(_dt(23)) == "asia"


def test_broker_server_authority_order():
    from execution_segments import broker_server_of
    assert broker_server_of({
        "verified_identity": {"broker_server": "ICM-Live04"},
        "expected_identity": {"broker_server": "Other"}}) == "ICM-Live04"
    assert broker_server_of({
        "expected_identity": {"broker_server": "Exp-Live"}}) == "Exp-Live"
    assert broker_server_of({}) == "unknown"


async def _scenario():
    os.environ["DB_NAME"] = DB_NAME
    import database
    database._client = None
    from database import get_db
    from execution_segments import record_fill, segments
    db = get_db()
    acct = {"_id": "seg_a1", "ea_version": "1.55",
            "verified_identity": {"broker_server": "ICM-Live04"}}
    try:
        # 12 true-slippage fills on one segment key → VERY_HIGH ready (10),
        # MAXIMUM not ready (20)
        for i in range(12):
            doc = await record_fill(
                db, trade={"_id": f"t{i}"}, account=acct,
                symbol="XAUUSD.m", side="BUY", slippage_pips=float(i),
                requested_price=2000.0, actual_price=2000.1)
        assert doc["symbol"] == "XAUUSD"          # broker suffix stripped
        assert doc["broker_server"] == "ICM-Live04"
        assert doc["true_slippage"] is True
        assert doc["session"] in ("asia", "london", "overlap", "newyork")
        # one legacy fill (no requested_price) must NOT count as evidence
        await record_fill(db, trade={"_id": "tl"}, account=acct,
                          symbol="XAUUSD", side="SELL", slippage_pips=99.0,
                          requested_price=None, actual_price=2000.0)

        out = await segments(db, days=7)
        assert out["total_fills"] == 13
        assert out["thresholds"] == {"VERY_HIGH": 10, "MAXIMUM": 20}
        seg = out["true_slippage_segments"]
        assert len(seg) == 1                      # legacy fill excluded
        s = seg[0]
        assert s["samples"] == 12
        assert s["broker_server"] == "ICM-Live04"
        assert s["symbol"] == "XAUUSD"
        assert s["median_pips"] == 6.0            # 0..11 → idx round(5.5)=6
        assert s["p95_pips"] == 10.0              # idx round(0.95*11)=10
        assert s["worst_pips"] == 11.0
        assert s["evidence_ready"] == {"VERY_HIGH": True, "MAXIMUM": False}
    finally:
        await db.client.drop_database(DB_NAME)


def test_record_and_segment_aggregation():
    asyncio.run(_scenario())
