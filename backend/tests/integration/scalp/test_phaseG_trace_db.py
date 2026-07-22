"""Phase G — trace assembly against the REAL database."""
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import os
import uuid

import pytest
from dotenv import load_dotenv

load_dotenv(_os.path.join(_BACKEND_DIR, ".env"))

pytestmark = pytest.mark.integration


def _db():
    from motor.motor_asyncio import AsyncIOMotorClient
    cli = AsyncIOMotorClient(os.environ["MONGO_URL"])
    return cli, cli[os.environ["DB_NAME"]]


@pytest.mark.asyncio
async def test_assemble_trace_end_to_end():
    import trade_trace as tt
    cli, db = _db()
    did = f"trace_test_{uuid.uuid4().hex}"
    tr_ins = await db.trades.insert_one({
        "scalp_decision_id": did, "symbol": "EURUSD", "status": "closed",
        "entry_price": 1.0851, "stop_loss": 1.0847, "profit": 0.9,
        "lifecycle_state": "CLOSED",
        "lifecycle": [{"state": "QUEUED", "at": "2026-06-01T10:00:00+00:00"}]})
    tid = str(tr_ins.inserted_id)
    await db.scalp_decisions.insert_one({
        "decision_id": did, "user_id": "trace_u", "symbol": "EURUSD",
        "direction": "BUY", "ts_ms": 1_000_000, "model_key": "b|d|EURUSD",
        "verdict": "live_traded",
        "features": {"spread_pips": 0.5},
        "forecast": {"p_target_before_stop": 0.6},
        "risk": {"lot": 0.01}})
    await db.trade_events.insert_one({
        "event_id": uuid.uuid4().hex, "event_type": "BrokerSubmitted",
        "decision_id": did, "trade_id": tid, "ts_ms": 1_005_000,
        "payload": {"lot": 0.01}})
    try:
        # resolves by decision_id AND by trade_id
        for key in (did, tid):
            out = await tt.assemble_trace(db, key)
            assert out is not None, key
            assert out["trace_id"] == did
            assert out["stages"]["oms"]["trade_id"] == tid
            assert out["event_count"] >= 1
            assert any("EURUSD BUY" in s for s in out["narrative"])
            assert any(x["what"] == "BrokerSubmitted"
                       for x in out["timeline"])
        assert await tt.assemble_trace(db, "no_such_trace") is None
    finally:
        await db.trades.delete_one({"_id": tr_ins.inserted_id})
        await db.scalp_decisions.delete_many({"decision_id": did})
        await db.trade_events.delete_many({"decision_id": did})
        cli.close()
