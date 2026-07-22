"""iter-142 · P0 audit fixes — protection-aware lifecycle, awaited
safety-critical writes, durable close intent, candle pipeline instrumentation."""
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import asyncio
import inspect
import re

from unittest.mock import AsyncMock, MagicMock

from scalp import order_state as os_


def _bridge_src():
    return open(_os.path.join(_BACKEND_DIR, "routes/bridge_routes.py")).read()


def _engine_src():
    import scalp.engine as e
    return inspect.getsource(e)


class TestProtectionLifecycleStates:            # P0-1
    def test_new_states_exist(self):
        for s in ("FILLED_UNPROTECTED", "PROTECTION_REQUESTED", "PROTECTED"):
            assert s in os_.STATES

    def test_transition_graph(self):
        assert os_.BROKER_ACCEPTED in os_.ALLOWED_PREV[os_.FILLED_UNPROTECTED]
        assert os_.ALLOWED_PREV[os_.PROTECTION_REQUESTED] == (os_.FILLED_UNPROTECTED,)
        assert os_.FILLED_UNPROTECTED in os_.ALLOWED_PREV[os_.PROTECTED]
        assert os_.PROTECTION_REQUESTED in os_.ALLOWED_PREV[os_.PROTECTED]
        # OPEN strictly requires confirmed protection (audit P0 round 2)
        assert os_.ALLOWED_PREV[os_.OPEN] == (os_.PROTECTED,)
        assert os_.FILLED_UNPROTECTED not in os_.ALLOWED_PREV[os_.OPEN]
        assert os_.BROKER_ACCEPTED not in os_.ALLOWED_PREV[os_.OPEN]

    def test_close_reachable_from_every_protection_state(self):
        for s in (os_.FILLED_UNPROTECTED, os_.PROTECTION_REQUESTED, os_.PROTECTED):
            assert s in os_.ALLOWED_PREV[os_.CLOSE_REQUESTED]
            assert s in os_.ALLOWED_PREV[os_.CLOSED]
            assert s in os_.ALLOWED_PREV[os_.FINANCIALLY_RECONCILED]

    def test_apply_accepts_protection_chain(self):
        async def run():
            db = MagicMock()
            seq = {"state": None}

            async def upd(q, u, **kw):
                res = MagicMock()
                new = u["$set"]["lifecycle_state"]
                allowed = []
                for clause in q.get("$or", []):
                    if isinstance(clause.get("lifecycle_state"), dict) \
                            and "$in" in clause["lifecycle_state"]:
                        allowed = clause["lifecycle_state"]["$in"]
                ok = seq["state"] is None or seq["state"] in allowed
                if ok:
                    seq["state"] = new
                    res.modified_count = 1
                else:
                    res.modified_count = 0
                return res
            db.trades.update_one = AsyncMock(side_effect=upd)
            db.trades.find_one = AsyncMock(
                side_effect=lambda *a, **k: {"lifecycle_state": seq["state"]})
            db.trade_events = MagicMock()
            db.trade_events.update_one = AsyncMock(return_value=MagicMock(upserted_id=None))
            assert await os_.apply(db, "64" * 12, os_.QUEUED, "k0") == "applied"
            for st in (os_.EA_CLAIMED, os_.BROKER_ACCEPTED, os_.FILLED_UNPROTECTED,
                       os_.PROTECTION_REQUESTED, os_.PROTECTED, os_.OPEN):
                assert await os_.apply(db, "64" * 12, st, f"k-{st}") == "applied", st
        asyncio.run(run())


class TestBridgeProtectionWiring:               # P0-1
    def test_fill_ack_goes_to_filled_unprotected(self):
        src = _bridge_src()
        assert "order_state.FILLED_UNPROTECTED" in src
        assert '"state": "AWAITING_CONFIRM"' in src
        # the fill ack must NOT jump straight to OPEN anymore — OPEN is only
        # applied by the heartbeat protection confirmation (_os.OPEN)
        assert "order_state.OPEN" not in src
        assert "_os.OPEN" in src

    def test_heartbeat_confirms_protection(self):
        src = _bridge_src()
        assert "_os.PROTECTED" in src
        assert "confirmed_stop_loss" in src
        assert "protection.confirmed_at" in src

    def test_missing_sl_rearms_then_escalates(self):
        src = _bridge_src()
        assert "protection_rearm" in src
        assert "unprotected_position" in src
        assert '"type": "FULL_CLOSE"' in src
        assert "age_s > 90" in src


class TestAwaitedSafetyWrites:                  # P0-2
    def test_no_bg_close_requests(self):
        src = _engine_src()
        assert not re.search(r"_bg\([^)]*_request_close", src)
        assert "await self._request_close(db, tid" in src

    def test_no_bg_slot_releases(self):
        src = _engine_src()
        assert not re.search(r"_bg\(lambda[^)]*release_broker_submission_slot", src)
        assert "await release_broker_submission_slot(db, slot)" in src

    def test_no_bg_reservation_release(self):
        src = _engine_src()
        assert not re.search(r'_bg\([^)]*release_for_trade', src)

    def test_no_bg_uncertainty_or_financials(self):
        src = _engine_src()
        assert '"uncertain_mark"' not in src
        assert "await self._persist_financial_event(" in src

    def test_lifecycle_methods_are_async(self):
        from scalp.engine import ScalpRunner
        for name in ("on_trade_opened", "on_close_ack", "on_trade_closed",
                     "on_partial_close", "_monitor_live_exits",
                     "_adaptive_manage", "_release_submission_slot_of"):
            assert asyncio.iscoroutinefunction(getattr(ScalpRunner, name)), name


class TestDurableClose:                         # P0-3
    def test_close_awaited_at_mark_time(self):
        src = _engine_src()
        # every _mark_close_requested is followed by an awaited request_close
        marks = src.count("self._mark_close_requested(")
        assert marks >= 3
        assert src.count("await self._request_close(") >= 4


class TestCandleInstrumentation:                # P0-4
    def test_candles_route_records_health(self):
        src = _bridge_src()
        assert "candle_feed_health" in src
        assert "bar_lag_s" in src
        assert "payloads_received" in src
        assert "last_write_ok" in src
        assert "dropped_bars" in src

    def test_freshness_api_exposes_candles(self):
        src = open(_os.path.join(_BACKEND_DIR, "routes/data_freshness_routes.py")).read()
        assert '"candles"' in src
        assert "candle_feed_health" in src
        assert '"candles":  20 * 60' in src
