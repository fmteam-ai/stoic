"""r25 P2-01 — close protocol: atomic per-trade sequence, one immutable ledger row
per (trade_id, close_seq), transactional wrapper, idempotent acknowledgement.
Runs against the real Mongo (MONGO_URL) so the unique index and race are exercised."""
import asyncio
import os
import uuid

import pytest
from motor.motor_asyncio import AsyncIOMotorClient

import close_commands as cc

pytestmark = pytest.mark.skipif(not os.environ.get("MONGO_URL"), reason="needs Mongo")


def _db():
    return AsyncIOMotorClient(os.environ["MONGO_URL"])[f"r25_close_{uuid.uuid4().hex[:8]}"]


async def _seed(db, n=3):
    await cc.ensure_indexes(db)
    ids = [f"t{i}_{uuid.uuid4().hex[:6]}" for i in range(n)]
    await db.trades.insert_many([{"_id": i, "status": "open", "symbol": "XAUUSD", "account_id": "a1",
                                  "mt5_ticket": 100 + k} for k, i in enumerate(ids)])
    return ids


def test_concurrent_requests_never_share_a_sequence():
    async def run():
        db = _db(); ids = await _seed(db)
        try:
            res = await asyncio.gather(*[cc.request_close(db, {"account_id": "a1"}, reason=f"r{k}", actor=f"w{k}")
                                         for k in range(6)])
            rows = await db.close_commands.find({}).to_list(1000)
            pairs = [(r["trade_id"], r["close_seq"]) for r in rows]
            assert len(pairs) == len(set(pairs)) == 6 * len(ids)            # unique index + atomic seq
            for tid in ids:
                seqs = sorted(r["close_seq"] for r in rows if r["trade_id"] == tid)
                assert seqs == list(range(1, 7)), seqs                         # monotonically increasing per trade
                latest = await db.trades.find_one({"_id": tid})
                assert latest["close_seq"] == 6
                # exactly one row still `requested` (the newest); all older ones superseded
                st = {r["close_seq"]: r["state"] for r in rows if r["trade_id"] == tid}
                assert st[6] == "requested" and all(st[s] == "superseded" for s in range(1, 6)), st
            assert all(r["trades_marked_for_close"] == len(ids) for r in res)
        finally:
            await db.client.drop_database(db.name)
    asyncio.run(run())


def test_ack_is_idempotent_and_seq_bound():
    async def run():
        db = _db(); ids = await _seed(db, 1)
        try:
            await cc.request_close(db, {"_id": ids[0]}, reason="manual", actor="ui")
            trade = await db.trades.find_one({"_id": ids[0]})
            a1 = await cc.acknowledge_close(db, trade, broker_deal_id="d1", occurred_at="2026-01-01T00:00:00Z")
            assert a1 and a1["state"] == "broker_confirmed" and a1["close_seq"] == 1
            trade = await db.trades.find_one({"_id": ids[0]})
            assert trade["close_command"]["state"] == "broker_confirmed"                 # pointer updated with the ack
            assert await cc.acknowledge_close(db, trade, broker_deal_id="d1", occurred_at="x") is None   # repeat → no-op
            stale = {**trade, "close_command": {**trade["close_command"], "state": "requested"}, "close_seq": 0}
            assert await cc.acknowledge_close(db, stale, broker_deal_id="d9", occurred_at="x") is None   # wrong seq → no-op
            row = await db.close_commands.find_one({"trade_id": ids[0]})
            assert row["broker_result"]["deal_id"] == "d1"
        finally:
            await db.client.drop_database(db.name)
    asyncio.run(run())


def test_no_open_trades_is_noop():
    async def run():
        db = _db()
        try:
            r = await cc.request_close(db, {"account_id": "none"}, reason="x", actor="y")
            assert r == {"command_id": None, "trades_marked_for_close": 0, "trade_ids": []}
        finally:
            await db.client.drop_database(db.name)
    asyncio.run(run())
