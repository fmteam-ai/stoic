"""Phase A — execution-engine completion: explicit order state machine,
full idempotency, transactional outbox, crash recovery. MongoDB-free."""
import inspect

import pytest
from unittest.mock import AsyncMock, MagicMock

from scalp import order_state as osm
from scalp import outbox

pytestmark = pytest.mark.unit


def _engine_src():
    from scalp import engine
    return inspect.getsource(engine)


class TestStateMachineRules:
    def test_states_and_terminals(self):
        assert set(osm.TERMINAL) == {"FINANCIALLY_RECONCILED", "REJECTED"}
        assert set(osm.ALLOWED_PREV) == set(osm.STATES)

    def test_happy_path_allowed(self):
        path = [osm.QUEUED, osm.EA_CLAIMED, osm.BROKER_ACCEPTED, osm.OPEN,
                osm.CLOSE_REQUESTED, osm.CLOSED, osm.FINANCIALLY_RECONCILED]
        for cur, new in zip(path, path[1:]):
            assert osm.can_transition(cur, new), f"{cur} -> {new}"

    def test_illegal_transitions_refused(self):
        assert not osm.can_transition(osm.CLOSED, osm.OPEN)
        assert not osm.can_transition(osm.FINANCIALLY_RECONCILED,
                                      osm.CLOSE_REQUESTED)
        assert not osm.can_transition(osm.OPEN, osm.EA_CLAIMED)
        assert not osm.can_transition(osm.OPEN, osm.QUEUED)

    def test_deal_may_arrive_before_ops_close(self):
        # authoritative broker deal can reconcile from ANY live state
        for cur in (osm.QUEUED, osm.EA_CLAIMED, osm.OPEN, osm.UNCERTAIN):
            assert osm.can_transition(cur, osm.FINANCIALLY_RECONCILED)

    def test_legacy_docs_may_enter_any_state(self):
        for st in osm.STATES:
            assert osm.can_transition(None, st)


class TestApplyIdempotency:
    def _db(self, modified=1, doc=None):
        db = MagicMock()
        res = MagicMock()
        res.modified_count = modified
        db.trades.update_one = AsyncMock(return_value=res)
        db.trades.find_one = AsyncMock(return_value=doc)
        return db

    @pytest.mark.asyncio
    async def test_applied(self):
        db = self._db(modified=1)
        out = await osm.apply(db, "aaaaaaaaaaaaaaaaaaaaaaaa",
                              osm.QUEUED, "q:1")
        assert out == "applied"
        q, u = db.trades.update_one.await_args.args
        assert q["lifecycle_keys"] == {"$ne": "q:1"}   # idempotency guard
        assert u["$set"]["lifecycle_state"] == "QUEUED"
        assert u["$addToSet"]["lifecycle_keys"] == "q:1"

    @pytest.mark.asyncio
    async def test_duplicate_key_is_noop(self):
        db = self._db(modified=0,
                      doc={"lifecycle_state": "QUEUED",
                           "lifecycle_keys": ["q:1"]})
        assert await osm.apply(db, "aaaaaaaaaaaaaaaaaaaaaaaa",
                               osm.QUEUED, "q:1") == "duplicate"

    @pytest.mark.asyncio
    async def test_invalid_transition_refused(self):
        db = self._db(modified=0,
                      doc={"lifecycle_state": "CLOSED",
                           "lifecycle_keys": []})
        assert await osm.apply(db, "aaaaaaaaaaaaaaaaaaaaaaaa",
                               osm.OPEN, "open:1") == "invalid"

    @pytest.mark.asyncio
    async def test_missing_trade(self):
        db = self._db(modified=0, doc=None)
        assert await osm.apply(db, "aaaaaaaaaaaaaaaaaaaaaaaa",
                               osm.OPEN, "k") == "missing"

    @pytest.mark.asyncio
    async def test_unknown_state_raises(self):
        with pytest.raises(ValueError):
            await osm.apply(self._db(), "x", "NOT_A_STATE", "k")


class TestOutbox:
    def _db(self):
        db = MagicMock()
        db.outbox.update_one = AsyncMock()
        db.outbox.create_index = AsyncMock()
        db.trade_events.update_one = AsyncMock()

        async def _aiter(*a, **k):
            return
            yield  # pragma: no cover
        find = MagicMock()
        find.sort.return_value.limit.return_value = _aiter()
        db.outbox.find.return_value = find
        return db

    @pytest.mark.asyncio
    async def test_enqueue_is_idempotent_insert(self):
        db = self._db()
        await outbox.enqueue(db, "trade_event", "k1", {"event_id": "e1"},
                             publish_now=False)
        q, u = db.outbox.update_one.await_args.args
        assert q == {"outbox_key": "k1"}
        assert "$setOnInsert" in u                     # never clobbers
        assert u["$setOnInsert"]["state"] == "pending"
        assert db.outbox.update_one.await_args.kwargs["upsert"] is True

    @pytest.mark.asyncio
    async def test_publish_dedupes_on_event_id(self):
        db = self._db()
        ok = await outbox._publish(db, {"topic": "trade_event",
                                        "payload": {"event_id": "e1"}})
        assert ok is True
        q, u = db.trade_events.update_one.await_args.args
        assert q == {"event_id": "e1"}
        assert "$setOnInsert" in u

    @pytest.mark.asyncio
    async def test_unknown_topic_stays_pending(self):
        db = self._db()
        assert await outbox._publish(db, {"topic": "??", "payload": {},
                                          "outbox_key": "k"}) is False


class TestPhaseAWiring:
    def test_engine_lifecycle_sites(self):
        src = _engine_src()
        assert "order_state.QUEUED" in src             # submit
        assert "order_state.UNCERTAIN" in src          # slot-link failure
        assert "order_state.CLOSE_REQUESTED" in src    # close intent
        assert "order_state.FINANCIALLY_RECONCILED" in src  # deal applied
        assert "def adopt_open_trade(" in src          # fill for unknown tid
        assert "async def _emit_durable(" in src       # outbox emitter
        assert 'await self._emit_durable(db, "BrokerSubmitted"' in src

    def test_queued_trades_restored_after_restart(self):
        src = _engine_src()
        assert '"status": {"$in": ["open", "pending"]}' in src
        assert '_st = "QUEUED"' in src

    def test_bridge_lifecycle_sites(self):
        src = open("/app/backend/routes/bridge_routes.py").read()
        assert "order_state.EA_CLAIMED" in src         # poll-trades dispatch
        assert "order_state.BROKER_ACCEPTED" in src    # fill ack
        # P0-1 · protection-aware lifecycle: fill → FILLED_UNPROTECTED;
        # heartbeat SL confirmation drives PROTECTED → OPEN
        assert "order_state.FILLED_UNPROTECTED" in src
        assert "_os.PROTECTED" in src
        assert "_os.OPEN" in src
        assert "_os.PROTECTION_REQUESTED" in src
        assert "order_state.CLOSED" in src
        assert "adopt_open_trade(trade)" in src

    def test_relay_wired_into_reconcile_loop(self):
        src = open("/app/backend/background_loops.py").read()
        assert "from scalp.outbox import relay_once" in src

    def test_seed_ensures_outbox_indexes(self):
        src = open("/app/backend/seed.py").read()
        assert "ensure_outbox_indexes" in src

    def test_critical_financial_events_not_double_emitted(self):
        src = _engine_src()
        # PositionClosed/FinancialApplied come ONLY from the durable path
        assert 'self._emit(db, "PositionClosed"' not in src
        assert 'self._emit(db, "FinancialApplied"' not in src
        assert 'key=f"te:PositionClosed:' in src
        assert 'key=f"te:FinancialApplied:' in src
