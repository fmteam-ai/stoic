"""Iter-63 — provisional risk reservations + production Origin fail-fast.
Pure unit tests (MagicMock db only)."""
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import inspect

import pytest
from unittest.mock import AsyncMock, MagicMock

from scalp import risk_reservations as rr

pytestmark = pytest.mark.unit


def _db():
    db = MagicMock()
    db.risk_reservations.insert_one = AsyncMock()
    db.risk_reservations.update_one = AsyncMock()
    db.risk_reservations.update_many = AsyncMock()
    db.risk_reservations.count_documents = AsyncMock(return_value=0)
    return db


class TestReservationLifecycle:
    @pytest.mark.asyncio
    async def test_reserve_shape(self):
        db = _db()
        r = await rr.reserve(db, account_id="a", user_id="u",
                             decision_id="d", risk_usd=10.567, lot=0.02)
        assert r["state"] == "RISK_RESERVED"
        assert r["risk_usd"] == 10.57
        assert r["uncertain"] is False and r["trade_id"] is None
        assert r["transitions"][0]["state"] == "RISK_RESERVED"
        db.risk_reservations.insert_one.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_transition_appends_audit(self):
        db = _db()
        db.risk_reservations.update_one = AsyncMock(
            return_value=MagicMock(modified_count=1))
        assert await rr.transition(db, "rid", "SLOT_LINKED") == "applied"
        args = db.risk_reservations.update_one.await_args.args
        # audit P1 · guarded: id + allowed-prev filter + idempotency key
        assert args[0]["reservation_id"] == "rid"
        assert args[0]["state"]["$in"] == ["RISK_RESERVED", "QUEUED_UNCONFIRMED"]
        assert args[1]["$set"]["state"] == "SLOT_LINKED"
        assert args[1]["$push"]["transitions"]["state"] == "SLOT_LINKED"

    @pytest.mark.asyncio
    async def test_release_for_trade_targets_active_only(self):
        # audit r3 · release now routes each reservation through the
        # guarded transition() instead of a raw update_many
        db = _db()
        docs = [{"reservation_id": "r1"}]

        def _find(*_a, **_k):
            async def gen():
                for d in docs:
                    yield d
            return gen()
        db.risk_reservations.find = MagicMock(side_effect=_find)
        db.risk_reservations.update_one = AsyncMock(
            return_value=MagicMock(modified_count=1))
        await rr.release_for_trade(db, "t1", "broker_ack")
        q = db.risk_reservations.find.call_args.args[0]
        assert q["trade_id"] == "t1"
        assert set(q["state"]["$in"]) == set(rr.ACTIVE_STATES)
        uq = db.risk_reservations.update_one.await_args.args[0]
        assert uq["reservation_id"] == "r1"
        assert set(uq["state"]["$in"]) == set(rr.ACTIVE_STATES)

    def test_states(self):
        assert rr.ACTIVE_STATES == ("RISK_RESERVED", "QUEUED_UNCONFIRMED",
                                    "SLOT_LINKED")
        assert rr.STALE_TTL_SEC == 900


class TestEngineWiring:
    def test_submit_live_reserves_and_transitions(self):
        from scalp import engine
        src = inspect.getsource(engine)
        assert "await risk_reservations.reserve(" in src
        assert '"QUEUED_UNCONFIRMED"' in src
        assert '"SLOT_LINKED"' in src
        assert "uncertain=True" in src
        # P0-2 · broker-ack reservation release is awaited inside
        # on_trade_opened (no longer a fire-and-forget lambda)
        assert 'await risk_reservations.release_for_trade(' in src
        assert '"broker_ack"' in src
        # reservation must exist BEFORE the order intent / queue
        assert src.index("await risk_reservations.reserve(") \
            < src.index('self._emit(db, "OrderIntentCreated"')

    def test_exposure_preflight_counts_unaccounted(self):
        from scalp import engine
        src = inspect.getsource(engine)
        assert "reserved_unaccounted = await risk_reservations" in src
        assert "db_open + reserved_unaccounted > self.account_risk.open_scalps" in src

    def test_reconcile_loop_sweeps_reservations(self):
        src = open(_os.path.join(_BACKEND_DIR, "background_loops.py")).read()
        assert "from scalp.risk_reservations import sweep_stale" in src


class TestProductionOriginFailFast:
    def test_startup_guard_present(self):
        src = open(_os.path.join(_BACKEND_DIR, "server.py")).read()
        assert 'os.environ.get("APP_ENV", "").lower() == "production"' in src
        assert "CSRF_ENFORCE_ORIGIN" in src
        assert "RuntimeError" in src
        # guard must run before any service starts
        assert src.index("APP_ENV") < src.index("await ensure_indexes()")
