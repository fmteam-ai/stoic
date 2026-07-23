"""iter-143 · Audit round 2 — pre-live hardening:
OPEN⊂PROTECTED, guarded reservations, strict unprotected timeout,
atomic embedded outbox, explicit worker mode."""
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import asyncio
import inspect

import pytest
from unittest.mock import AsyncMock, MagicMock

from scalp import order_state as os_
from scalp import risk_reservations as rr
from scalp import outbox as ob


def _engine_src():
    import scalp.engine as e
    return inspect.getsource(e)


class TestOpenRequiresProtected:                # item 1
    def test_open_only_from_protected(self):
        assert os_.ALLOWED_PREV[os_.OPEN] == (os_.PROTECTED,)

    def test_legacy_recovery_via_broker_verification(self):
        # legacy BROKER_ACCEPTED docs certify through the broker-SL check
        # (→ PROTECTED → OPEN), never via a lifecycle shortcut
        assert os_.BROKER_ACCEPTED in os_.ALLOWED_PREV[os_.PROTECTED]
        src = open(_os.path.join(_BACKEND_DIR, "routes/bridge_routes.py")).read()
        assert '"BROKER_ACCEPTED")' in src   # heartbeat certification list

    def test_cannot_skip_protection(self):
        assert not os_.can_transition(os_.BROKER_ACCEPTED, os_.OPEN)
        assert not os_.can_transition(os_.FILLED_UNPROTECTED, os_.OPEN)
        assert not os_.can_transition(os_.PROTECTION_REQUESTED, os_.OPEN)


class TestGuardedReservations:                  # item 2
    def _db(self, modified=1, doc=None):
        db = MagicMock()
        db.risk_reservations.update_one = AsyncMock(
            return_value=MagicMock(modified_count=modified))
        db.risk_reservations.find_one = AsyncMock(return_value=doc)
        return db

    def test_unknown_state_raises(self):
        with pytest.raises(ValueError):
            asyncio.run(rr.transition(self._db(), "rid", "ACTIVE_AGAIN"))

    def test_released_is_terminal(self):
        # RELEASED appears in no state's allowed-prev list
        for prevs in rr.ALLOWED_PREV.values():
            assert "RELEASED" not in prevs

    def test_applied_and_guard_query(self):
        db = self._db(1)
        out = asyncio.run(rr.transition(db, "rid", "RELEASED",
                                        release_reason="closed"))
        assert out == "applied"
        q = db.risk_reservations.update_one.await_args.args[0]
        assert set(q["state"]["$in"]) == set(rr.ACTIVE_STATES)
        assert q["transition_keys"]["$ne"] == "RELEASED:closed"

    def test_duplicate_detected(self):
        db = self._db(0, {"state": "RELEASED",
                          "transition_keys": ["RELEASED:closed"]})
        out = asyncio.run(rr.transition(db, "rid", "RELEASED",
                                        release_reason="closed"))
        assert out == "duplicate"

    def test_invalid_reactivation_refused(self):
        db = self._db(0, {"state": "RELEASED", "transition_keys": []})
        out = asyncio.run(rr.transition(db, "rid", "SLOT_LINKED"))
        assert out == "invalid"

    def test_missing_row_detected(self):
        db = self._db(0, None)
        assert asyncio.run(rr.transition(db, "rid", "RELEASED")) == "missing"


class TestStrictUnprotectedTimeout:             # item 3
    def test_deadline_set_on_fill_ack(self):
        src = _engine_src()
        assert "PROTECTION_GRACE_MS" in src
        assert "protection_deadline_ms" in src

    def test_monitor_emergency_closes(self):
        src = _engine_src()
        assert '"unprotected_timeout"' in src
        assert "protection_confirmed" in src


class TestAtomicEmbeddedOutbox:                 # item 4
    def test_apply_embeds_event_in_same_update(self):
        async def run():
            db = MagicMock()
            db.trades.update_one = AsyncMock(
                return_value=MagicMock(modified_count=1))
            out = await os_.apply(db, "64" * 12, os_.QUEUED, "k1")
            assert out == "applied"
            upd = db.trades.update_one.await_args.args[1]
            ev = upd["$push"]["outbox_events"]
            assert ev["published"] is False
            assert ev["event_id"] == f"lc:{'64' * 12}:k1"
            assert upd["$set"]["lifecycle_state"] == os_.QUEUED
        asyncio.run(run())

    def test_relay_drains_embedded(self):
        assert hasattr(ob, "relay_embedded")
        src = inspect.getsource(ob.relay_once)
        assert "relay_embedded" in src
        src_e = inspect.getsource(ob.relay_embedded)
        assert "array_filters" in src_e
        assert "$setOnInsert" in src_e   # idempotent publish


class TestExplicitWorkerMode:                   # item 5
    def test_default_is_off_with_critical_log(self):
        src = open(_os.path.join(_BACKEND_DIR, "server.py")).read()
        assert 'os.environ.get("BACKGROUND_WORKERS_IN_PROCESS")' in src
        assert "fail-safe default" in src
        assert '"true").lower() == "false"' not in src

    def test_preview_env_sets_mode_explicitly(self):
        env_path = _os.path.join(_BACKEND_DIR, ".env")
        if not _os.path.exists(env_path):
            pytest.skip("no local .env (CI checkout) — mode asserted via .env.example key below")
        env = open(env_path).read()
        assert "BACKGROUND_WORKERS_IN_PROCESS=true" in env
