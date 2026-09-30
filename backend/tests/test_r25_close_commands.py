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


@pytest.fixture(autouse=True)
def synthetic_posture(monkeypatch):
    """The sandbox mongod is standalone: the fresh per-test DB has zero live-like
    accounts, so the verified synthetic-only posture permits the non-transactional path."""
    monkeypatch.setenv("NL_EFFECTS_SYNTHETIC_ONLY", "true")
    monkeypatch.setenv("APP_ENV", "preview")


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
            assert r == {"command_id": None, "trades_marked_for_close": 0, "trade_ids": [], "commands": []}
        finally:
            await db.client.drop_database(db.name)
    asyncio.run(run())


def test_fenced_modification_is_stamped_in_the_same_atomic_update():
    async def run():
        db = _db(); ids = await _seed(db, 1)
        try:
            out = await cc.request_close(db, {"_id": ids[0]}, reason="scalp_x", actor="scalp",
                                         pending_modification={"type": "FULL_CLOSE", "reason": "scalp_x"})
            t = await db.trades.find_one({"_id": ids[0]})
            pm = t["pending_modification"]
            assert pm["type"] == "FULL_CLOSE" and len(pm["intent_id"]) == 32
            assert pm["seq"] == t["command_seq"] == 1 and t["close_seq"] == 1
            assert pm["close_command_key"] == out["command_id"]
            assert out["commands"][0]["pending_modification"]["intent_id"] == pm["intent_id"]
            row = await db.close_commands.find_one({"trade_id": ids[0]})
            assert row["pending_modification"]["intent_id"] == pm["intent_id"]
            # a second command bumps BOTH sequences and supersedes the first row
            await cc.request_close(db, {"_id": ids[0]}, reason="manual", actor="ui")
            t = await db.trades.find_one({"_id": ids[0]})
            assert t["close_seq"] == 2 and t["command_seq"] == 1          # no modification → command_seq untouched
            assert t["status"] == "open"                                   # never flipped to pending
            states = {r["close_seq"]: r["state"] async for r in db.close_commands.find({"trade_id": ids[0]})}
            assert states == {1: "superseded", 2: "requested"}
        finally:
            await db.client.drop_database(db.name)
    asyncio.run(run())


def test_capital_capable_deployment_fails_closed_without_transactions(monkeypatch):
    async def run():
        db = _db(); ids = await _seed(db, 1)
        try:
            # a terminal-bound account makes the deployment capital-capable → no standalone fallback
            await db.accounts.insert_one({"status": "connected", "bridge_token": "bt_live", "mode": "live"})
            with pytest.raises(cc.TransactionsUnavailable):
                await cc.request_close(db, {"_id": ids[0]}, reason="manual", actor="ui")
            t = await db.trades.find_one({"_id": ids[0]})
            assert not t.get("close_requested") and await db.close_commands.count_documents({}) == 0   # nothing written
            # acknowledgement is guarded by the same rule
            with pytest.raises(cc.TransactionsUnavailable):
                await cc.acknowledge_close(db, {"_id": ids[0], "close_seq": 1,
                                                "close_command": {"key": "k", "state": "requested"}},
                                           broker_deal_id="d", occurred_at="x")
        finally:
            await db.client.drop_database(db.name)
    asyncio.run(run())


def test_production_never_falls_back(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    async def run():
        db = _db(); ids = await _seed(db, 1)
        try:
            with pytest.raises(cc.TransactionsUnavailable):
                await cc.request_close(db, {"_id": ids[0]}, reason="manual", actor="ui")
        finally:
            await db.client.drop_database(db.name)
    asyncio.run(run())


def test_panic_emergency_path_never_refuses_but_raises_an_incident():
    async def run():
        db = _db(); ids = await _seed(db, 1)
        try:
            await db.accounts.insert_one({"status": "connected", "bridge_token": "bt_live", "mode": "live"})  # capital-capable
            out = await cc.request_close(db, {"_id": ids[0]}, reason="panic", actor="panic:test", emergency=True)
            assert out["trades_marked_for_close"] == 1
            inc = await db.close_protocol_incidents.find_one({"kind": "panic_non_transactional"})
            assert inc and "replica-set" in inc["resolution"]
        finally:
            await db.client.drop_database(db.name)
    asyncio.run(run())
