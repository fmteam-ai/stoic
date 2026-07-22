"""Round 18 review — scalp lifecycle hardening.

1  Safety-critical reservation transitions are AWAITED (not fire-and-forget).
2  Adaptive stops: pending vs broker-CONFIRMED state, ack via bridge.
3  Adaptive stop modifications snap to the instrument tick grid.
4  Order lifecycle: QUEUED until broker fill ack, only then OPEN.
5  Close intents are durable (persist-retry until the DB write confirms).
7  Position-conditioned exit probability (not a fresh-entry score).
8  Reservation docs: native datetimes, active flag, unique DB constraints.

Pure unit tests — no MongoDB.
"""
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import inspect
import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock

from scalp import adaptive_exits as ax
from scalp import risk_reservations as rr

pytestmark = pytest.mark.unit
PIP = 0.0001


def _engine_src():
    from scalp import engine
    return inspect.getsource(engine)


class TestAwaitedReservationTransitions:          # item 1
    def test_submit_path_awaits_all_transitions(self):
        src = _engine_src()
        assert "async def _resv_transition(" in src
        assert src.count("await _resv_transition(") >= 4
        # no reservation transition may remain a background write
        assert "risk_reservations.transition(db, r," not in src

    def test_deal_financials_awaits_release(self):
        src = _engine_src()
        assert 'await risk_reservations.release_for_trade(\n                db, str(trade["_id"]), "closed")' in src

    def test_bridge_report_awaits_broker_ack_release(self):
        # P0-2 · release moved INSIDE the awaited on_trade_opened
        src = open(_os.path.join(_BACKEND_DIR, "routes/bridge_routes.py")).read()
        assert "await r.on_trade_opened(" in src
        esrc = _engine_src()
        assert "await risk_reservations.release_for_trade(" in esrc
        assert '"broker_ack"' in esrc


class TestPendingVsConfirmedStop:                 # item 2
    def test_engine_uses_confirmed_stop_only(self):
        src = _engine_src()
        assert 'info.get("confirmed_stop_px") or info["stop_px"]' in src
        assert "pending_stop_px" in src
        assert "pending_stop_request_id" in src
        assert "adaptive_stop_px" not in src      # legacy field gone

    def test_ack_hook_exists(self):
        src = _engine_src()
        assert "def on_stop_modified(" in src
        bridge = open(_os.path.join(_BACKEND_DIR, "routes/bridge_routes.py")).read()
        assert "on_stop_modified(" in bridge

    def test_on_stop_modified_confirms_and_rejects(self):
        from scalp.engine import ScalpRunner
        r = ScalpRunner.__new__(ScalpRunner)
        r.live_trades = {"t1": {"stop_px": 1.08, "pending_stop_px": 1.0820,
                                "pending_stop_request_id": "rq",
                                "pending_stop_ms": 1}}
        r.on_stop_modified("t1", 1.0820, True, db=None)
        info = r.live_trades["t1"]
        assert info["confirmed_stop_px"] == 1.0820
        assert "pending_stop_px" not in info
        # rejected modification leaves the confirmed stop untouched
        info["pending_stop_px"] = 1.0830
        r.on_stop_modified("t1", None, False, db=None)
        assert info["confirmed_stop_px"] == 1.0820
        assert "pending_stop_px" not in info


class TestTickGridRounding:                       # item 3
    def test_no_fixed_five_decimal_round(self):
        src = _engine_src()
        assert "round(s, 5)" not in src
        assert "new_sl = round_to_tick(new_sl, tick)" in src


class TestOrderLifecycle:                         # item 4
    def test_queued_until_broker_ack(self):
        src = _engine_src()
        assert '"state": "QUEUED"' in src
        assert "queued_ms" in src
        assert '"queued_timeout"' in src          # queue failsafe

    def test_fill_ack_promotes_to_open(self):
        from scalp.engine import ScalpRunner
        r = ScalpRunner.__new__(ScalpRunner)
        r.live_trades = {"t1": {"state": "QUEUED", "queued_ms": 1,
                                "opened_ms": 1, "direction": "BUY"}}
        r.exec_fills = 0
        asyncio.run(r.on_trade_opened("t1", None, None, db=None))
        info = r.live_trades["t1"]
        assert info["state"] == "OPEN"
        assert info["broker_ack_ms"] == info["opened_ms"]  # clock from fill


class TestDurableCloseIntent:                     # item 5
    def test_close_persist_retry_wired(self):
        src = _engine_src()
        assert "close_persisted" in src
        assert "close_reason_pending" in src
        # P0-2 · retry is now awaited inline instead of a _bg lambda
        assert "await self._request_close(db, tid, rs)" in src
        assert "def _mark_close_requested(" in src


class TestPositionPTarget:                        # item 7
    def test_barrier_extremes(self):
        common = dict(direction="BUY", entry_px=1.0850, pip_size=PIP,
                      stop_px=1.0847, target_px=1.0855)
        assert ax.position_p_target(mid=1.0856, **common) == 0.98  # past tgt
        assert ax.position_p_target(mid=1.0846, **common) == 0.02  # past stop

    def test_driftless_base_is_barrier_ratio(self):
        # equidistant: 2p to stop, 2p to target → 0.5
        p = ax.position_p_target(direction="BUY", entry_px=1.0850,
                                 mid=1.0850, stop_px=1.0848,
                                 target_px=1.0852, pip_size=PIP)
        assert p == pytest.approx(0.5)

    def test_model_tilt_shifts_probability(self):
        kw = dict(direction="SELL", entry_px=1.0850, mid=1.0850,
                  stop_px=1.0852, target_px=1.0848, pip_size=PIP)
        assert ax.position_p_target(p_model=0.7, **kw) > \
            ax.position_p_target(p_model=0.3, **kw)

    def test_time_capacity_discounts_far_targets(self):
        kw = dict(direction="BUY", entry_px=1.0850, mid=1.0850,
                  stop_px=1.0845, target_px=1.0855, pip_size=PIP,
                  vol_short_pips=3.0, max_holding_ms=300_000)
        # nearly out of time, low vol, 5 pips to go → heavily discounted
        late = ax.position_p_target(elapsed_ms=290_000, **kw)
        early = ax.position_p_target(elapsed_ms=0, **kw)
        assert late < early

    def test_mae_penalty(self):
        kw = dict(direction="BUY", entry_px=1.0850, mid=1.0849,
                  stop_px=1.0846, target_px=1.0855, pip_size=PIP)
        hurt = ax.position_p_target(mae_frac=0.9, mfe_frac=0.05, **kw)
        fine = ax.position_p_target(mae_frac=0.1, mfe_frac=0.5, **kw)
        assert hurt < fine

    def test_engine_feeds_position_p_not_fresh_score(self):
        src = _engine_src()
        assert "adaptive_exits.position_p_target(" in src
        assert "p_target=p_pos" in src
        assert 'info.get("mfe_r")' in src and 'info.get("mae_r")' in src


def _db():
    db = MagicMock()
    db.risk_reservations.insert_one = AsyncMock()
    db.risk_reservations.update_one = AsyncMock()
    db.risk_reservations.update_many = AsyncMock()
    db.risk_reservations.create_index = AsyncMock()
    return db


class TestReservationDocHardening:                # item 8
    @pytest.mark.asyncio
    async def test_native_datetimes_and_active_flag(self):
        from datetime import datetime
        db = _db()
        doc = await rr.reserve(db, account_id="a", user_id="u",
                               decision_id="d", risk_usd=5, lot=0.01)
        assert doc["active"] is True
        assert isinstance(doc["created_at"], datetime)
        assert isinstance(doc["updated_at"], datetime)
        assert isinstance(doc["transitions"][0]["at"], datetime)

    @pytest.mark.asyncio
    async def test_release_clears_active(self):
        db = _db()
        ok = MagicMock(modified_count=1)
        db.risk_reservations.update_one = AsyncMock(return_value=ok)
        assert await rr.transition(db, "rid", "RELEASED",
                                   release_reason="closed") == "applied"
        upd = db.risk_reservations.update_one.await_args.args[1]["$set"]
        assert upd["active"] is False
        assert await rr.transition(db, "rid", "SLOT_LINKED") == "applied"
        upd = db.risk_reservations.update_one.await_args.args[1]["$set"]
        assert upd["active"] is True
        # audit P1 · guard contract: transitions filter on allowed prevs
        # and idempotency keys
        q = db.risk_reservations.update_one.await_args.args[0]
        assert q["state"]["$in"] == ["RISK_RESERVED", "QUEUED_UNCONFIRMED"]
        assert "$ne" in q["transition_keys"]
        # audit r3 · release_for_trade goes through guarded transition()
        def _find(*_a, **_k):
            async def gen():
                yield {"reservation_id": "rid"}
            return gen()
        db.risk_reservations.find = MagicMock(side_effect=_find)
        await rr.release_for_trade(db, "t1", "closed")
        upd = db.risk_reservations.update_one.await_args.args[1]["$set"]
        assert upd["active"] is False
        assert upd["state"] == "RELEASED"

    @pytest.mark.asyncio
    async def test_index_contract(self):
        db = _db()
        await rr.ensure_reservation_indexes(db)
        calls = db.risk_reservations.create_index.await_args_list
        assert len(calls) == 4
        # unique reservation_id
        assert calls[0].args == ("reservation_id",)
        assert calls[0].kwargs["unique"] is True
        # one ACTIVE reservation per decision
        assert calls[1].kwargs["partialFilterExpression"] == {"active": True}
        # one ACTIVE reservation per linked (string) trade_id
        assert calls[2].kwargs["partialFilterExpression"]["trade_id"] == \
            {"$type": "string"}
        # sweep/monitor compound index
        assert calls[3].args[0] == [("account_id", 1), ("state", 1),
                                    ("updated_at", -1)]

    def test_seed_wires_reservation_indexes(self):
        src = open(_os.path.join(_BACKEND_DIR, "seed.py")).read()
        assert "ensure_reservation_indexes" in src
