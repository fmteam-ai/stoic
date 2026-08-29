"""iter-149 adversarial: cache-tamper, forced-age recompute, fail-safe.

Verifies the three integrity properties beyond the happy-path lifecycle:

1. Tampering the maintained payload (e.g. wiping factor_lots) but ALSO
   inserting a trade → basis mismatch → recompute OVERWRITES tampered data.
2. Setting risk_state._max_age_s to 0 forces recompute every read (age gate).
3. When pamm_risk_state operations RAISE (broken cache), _telemetry still
   produces correct evidence via the full recompute path — never crashes,
   never fabricates values.
"""
import asyncio
import os
import uuid
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.integration

DB_NAME = f"stoic_test_riskstate_adv_{uuid.uuid4().hex[:8]}"


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
    acct_id = "riskstate_adv_acct"
    account = {"_id": acct_id, "balance": 10000.0}
    signal = {"symbol": "EURUSD", "action": "BUY", "lot_size": 0.1}

    try:
        # Seed 1 open trade — factor_lots EUR +1, USD -1
        await db.trades.insert_one({
            "account_id": acct_id, "status": "open", "symbol": "EURUSD",
            "action": "BUY", "lot_size": 1.0, "risk_pct": 1.0,
        })
        # Prime cache
        t1 = await _telemetry(db, {}, account, signal)
        assert t1["risk_state_source"] == "recomputed"
        assert t1["factor_lots"].get("EUR") == 1.0
        assert t1["factor_lots"].get("USD") == -1.0

        # (A) Tamper the stored payload — wipe factor_lots — WITHOUT changing
        # trades. Since basis is unchanged and fresh, this would be served —
        # but we IMMEDIATELY invalidate the basis by inserting another trade,
        # which is the guaranteed property: basis mismatch → recompute.
        await db.pamm_risk_state.update_one(
            {"account_id": acct_id},
            {"$set": {"payload.factor_lots": {},
                      "payload.symbol_lots": {},
                      "payload.open_risk_pct_sum": 0.0}})
        # Sanity: the tampered payload is currently in the store
        doc = await db.pamm_risk_state.find_one({"account_id": acct_id})
        assert doc["payload"]["factor_lots"] == {}

        # Add a new open trade — basis.open_count now 2 → mismatch → recompute
        await db.trades.insert_one({
            "account_id": acct_id, "status": "open", "symbol": "GBPUSD",
            "action": "BUY", "lot_size": 0.5, "risk_pct": 0.5,
        })
        t2 = await _telemetry(db, {}, account, signal)
        assert t2["risk_state_source"] == "recomputed"
        assert t2["open_positions"] == 2
        # factor_lots recomputed from actual trades: EUR +1, GBP +0.5, USD -1.5
        assert t2["factor_lots"].get("EUR") == 1.0
        assert t2["factor_lots"].get("GBP") == 0.5
        assert t2["factor_lots"].get("USD") == -1.5
        # And the tampered payload got OVERWRITTEN in storage
        doc2 = await db.pamm_risk_state.find_one({"account_id": acct_id})
        assert doc2["payload"]["factor_lots"].get("EUR") == 1.0
        assert doc2["payload"]["factor_lots"].get("USD") == -1.5

        # (B) Force age-based recompute via monkeypatch: max_age=0 means
        # every stored doc is considered stale → recompute every read.
        original_max_age = risk_state._max_age_s
        risk_state._max_age_s = lambda: 0
        try:
            t3 = await _telemetry(db, {}, account, signal)
            assert t3["risk_state_source"] == "recomputed", (
                "max_age=0 must force recompute")
            t4 = await _telemetry(db, {}, account, signal)
            assert t4["risk_state_source"] == "recomputed"
        finally:
            risk_state._max_age_s = original_max_age

        # After restoring, next read is maintained again (basis unchanged)
        t5 = await _telemetry(db, {}, account, signal)
        assert t5["risk_state_source"] == "maintained"

        # (C) Fail-safe: force compute_basis to raise → _telemetry MUST still
        # produce correct evidence via full recompute path, no crash.
        original_basis = risk_state.compute_basis

        async def _broken_basis(*a, **kw):
            raise RuntimeError("simulated cache infra failure")

        risk_state.compute_basis = _broken_basis
        try:
            t6 = await _telemetry(db, {}, account, signal)
            # Falls through to recompute (cached=None because basis is None)
            assert t6["risk_state_source"] == "recomputed"
            # Evidence is still correct despite broken basis helper
            assert t6["open_positions"] == 2
            assert t6["factor_lots"].get("EUR") == 1.0
            assert t6["factor_lots"].get("USD") == -1.5
            assert t6["factor_lots"].get("GBP") == 0.5
            assert t6["open_risk_pct_sum"] == 1.5
        finally:
            risk_state.compute_basis = original_basis
    finally:
        await db.client.drop_database(DB_NAME)
        database._client = None
        risk_state._indexes_ready = False


def test_riskstate_adversarial_and_failsafe():
    asyncio.new_event_loop().run_until_complete(_scenario())
