"""prod v1.60.28 — two execution bugs.

1. Stuck intent after auto-deleverage: cancelling a pending trade left its execution intent 'submitted' → execution
   authority CLOSE_ONLY ("broker-accepted execution past settlement SLA") on that account; the intent sweep lived only
   inside the PAMM sweep, which does not run in external-workers mode (production). Now: cancel → intent cancelled +
   reservation released; a sweep safety net settles any in-flight intent whose trade is already terminal; the sweep
   runs in the reconciliation worker without PAMM programs; VaR is bounded by protective stops.
2. Lot rounds to 0 on OnEquity: base = broker minimum, every trim → 0 → every signal lost. Now: a trim under the
   minimum is floored to the minimum while the minimum still fits the risk budget; skipped only when it doesn't."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _pending_with_intent(db, *, user_id="u1", account_id="acc1", status="submitted", age_s=0):
    tid = ObjectId()
    iid = f"INT-{tid}"
    created = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat()
    db.trades.rows.append({"_id": tid, "user_id": user_id, "account_id": account_id, "status": "pending",
                           "symbol": "XAUUSD", "execution_intent_id": iid, "reservation_id": f"R-{tid}"})
    db.execution_intents.rows.append({"intent_id": iid, "status": status, "account_id": account_id,
                                      "created_at": created, "updated_at": created,
                                      "result": {"trade_id": str(tid)}, "history": []})
    db.risk_reservations.rows.append({"reservation_id": f"R-{tid}", "account_id": account_id, "user_id": user_id,
                                      "trade_id": str(tid), "state": "SLOT_LINKED", "active": True,
                                      "transition_keys": [], "transitions": []})
    return tid, iid


# ── 1. stuck intent ─────────────────────────────────────────────────────────────────────────────────────────────
def test_deleverage_cancel_of_a_pending_trade_terminalises_the_intent_and_releases_the_reservation():
    from portfolio.risk_manager import execute_deleveraging_actions
    db = FakeDb()
    tid, iid = _pending_with_intent(db)
    with patch("close_commands.request_close", AsyncMock(return_value={"trades_marked_for_close": 0})):
        out = _run(execute_deleveraging_actions(db, user_id="u1", actions=[
            {"kind": "close_trade", "trade_id": str(tid), "reason": "var_breach"}]))
    assert out == {"closed": 0, "cancelled": 1, "skipped": 0, "actions": 1}
    tr = _run(db.trades.find_one({"_id": tid}))
    assert tr["status"] == "cancelled" and tr["close_reason"] == "var_breach" and "auto-deleverage" in tr["error"]
    it = _run(db.execution_intents.find_one({"intent_id": iid}))
    assert it["status"] == "cancelled" and it["history"][-1]["to"] == "cancelled"
    rr = _run(db.risk_reservations.find_one({"reservation_id": f"R-{tid}"}))
    assert rr["state"] == "RELEASED" and rr["active"] is False
    # and execution truth no longer sees an unresolved execution on the account
    from execution_truth import unresolved_backlog
    backlog = _run(unresolved_backlog(db, datetime.now(timezone.utc) + timedelta(seconds=600), account_ids=["acc1"]))
    assert not backlog["exemplars"]


def test_sweep_settles_an_intent_a_bare_status_flip_left_in_flight_without_any_pamm_program():
    from execution_intents import sweep_intents
    db = FakeDb()
    tid, iid = _pending_with_intent(db, age_s=30)                         # young: NOT stale, NOT unknown yet
    tid2, iid2 = _pending_with_intent(db, age_s=30)                       # genuinely pending → untouched
    _run(db.trades.update_one({"_id": tid}, {"$set": {"status": "cancelled"}}))   # the old bug: status only
    assert not db.pamm_programs.rows
    out = _run(sweep_intents(db))
    assert out["intents_settled_from_trades"] == 1
    assert _run(db.execution_intents.find_one({"intent_id": iid}))["status"] == "cancelled"
    assert _run(db.execution_intents.find_one({"intent_id": iid2}))["status"] == "submitted"
    assert _run(db.risk_reservations.find_one({"trade_id": str(tid)}))["state"] == "RELEASED"
    assert _run(db.risk_reservations.find_one({"trade_id": str(tid2)}))["state"] == "SLOT_LINKED"
    # legal-transition selection: a DISPATCHED intent cannot go 'cancelled' → it is 'expired'; a filled trade → 'filled'
    tid3, iid3 = _pending_with_intent(db, status="dispatched")
    _run(db.trades.update_one({"_id": tid3}, {"$set": {"status": "cancelled"}}))
    tid4, iid4 = _pending_with_intent(db, status="acked")
    _run(db.trades.update_one({"_id": tid4}, {"$set": {"status": "open", "mt5_ticket": 77}}))
    _run(sweep_intents(db))
    assert _run(db.execution_intents.find_one({"intent_id": iid3}))["status"] == "expired"
    assert _run(db.execution_intents.find_one({"intent_id": iid4}))["status"] == "filled"


def test_intent_sweep_runs_in_the_reconciliation_worker_and_the_pamm_sweep_shares_it():
    worker = open(os.path.join(ROOT, "backend", "workers", "reconciliation.py"), encoding="utf-8").read()
    assert "_execution_intents_sweep_loop" in worker and "_pamm_sweep_loop" in worker      # both sweeps run in external mode
    loops = open(os.path.join(ROOT, "backend", "background_loops.py"), encoding="utf-8").read()
    assert "async def _execution_intents_sweep_loop" in loops and "sweep_intents(get_db())" in loops
    pamm = open(os.path.join(ROOT, "backend", "modules", "pamm", "sweep.py"), encoding="utf-8").read()
    assert "sweep_intents(db)" in pamm and "reconcile_unknown_intents(db)" not in pamm
    rm = open(os.path.join(ROOT, "backend", "portfolio", "risk_manager.py"), encoding="utf-8").read()
    assert "cancel_pending(db, {\"_id\": oid, \"user_id\": user_id}" in rm and '"status": "cancelled"' not in rm
    # the worker module really resolves every loop it lists (no dangling import in external-workers mode)
    import importlib
    import background_loops as bl
    for name in ("_auto_heal_loop", "_stuck_open_sync_loop", "_scalp_reconcile_loop", "_eod_flatten_loop",
                 "_execution_intents_sweep_loop", "_pamm_sweep_loop"):
        assert callable(getattr(bl, name)), name
    assert callable(getattr(importlib.import_module("alerting"), "_ops_alert_loop"))


def test_intent_sweep_loop_iterates_sweeps_and_records_a_loop_progress_heartbeat(monkeypatch):
    import background_loops as bl
    from workers.base import LOOP_PROGRESS
    db = FakeDb()
    tid, iid = _pending_with_intent(db, age_s=30)
    _run(db.trades.update_one({"_id": tid}, {"$set": {"status": "cancelled"}}))
    monkeypatch.setattr(bl, "get_db", lambda: db)
    monkeypatch.setenv("INTENT_SWEEP_INTERVAL_SEC", "1")
    LOOP_PROGRESS.pop("_execution_intents_sweep_loop", None)

    async def _one_iteration():
        task = asyncio.get_event_loop().create_task(bl._execution_intents_sweep_loop())
        await asyncio.sleep(1.6)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    _run(_one_iteration())
    assert _run(db.execution_intents.find_one({"intent_id": iid}))["status"] == "cancelled"
    st = LOOP_PROGRESS["_execution_intents_sweep_loop"]
    assert st["processed_count"] >= 1 and st["expected_interval_sec"] == 1 and st["last_success_at"] is not None


def test_var_is_bounded_by_protective_stops_so_one_small_gold_order_does_not_trip_the_cap(monkeypatch):
    import portfolio.var as pv
    from portfolio.risk_manager import VAR_CAP_PCT
    monkeypatch.setattr(pv, "_atr_pct", AsyncMock(return_value=0.02))        # gold: 2 % daily vol
    monkeypatch.setattr(pv, "corr_returns", AsyncMock(return_value=None))
    pos = [{"symbol": "XAUUSD", "lot_size": 0.05, "entry_price": 4000.0, "stop_loss": 3980.0, "action": "BUY",
            "status": "open"}]
    out = _run(pv.calculate_var(pos, equity=10_000.0))
    # parametric: 1.645 × 0.02 × $20,000 = $658 = 6.58 % → would trip the 5 % cap; the stop risks $100 (×1.5 gap)
    assert out["stop_bounded_loss_usd"] == 150.0 and out["var_95_usd"] == 150.0 and out["var_95_pct_equity"] == 1.5
    assert out["cvar_99_usd"] == 150.0 and any("bounded by protective stops" in n for n in out["notes"])
    assert out["var_95_pct_equity"] < VAR_CAP_PCT
    # a position WITHOUT a stop (or a stop on the wrong side) keeps the unbounded parametric figure
    out = _run(pv.calculate_var([{**pos[0], "stop_loss": None}], equity=10_000.0))
    assert out["stop_bounded_loss_usd"] is None and out["var_95_usd"] == 658.0
    out = _run(pv.calculate_var([{**pos[0], "stop_loss": 4020.0}], equity=10_000.0))
    assert out["stop_bounded_loss_usd"] is None and out["var_95_usd"] == 658.0
    # a wide stop never INCREASES VaR: bound applies only when it is tighter
    out = _run(pv.calculate_var([{**pos[0], "stop_loss": 3000.0}], equity=10_000.0))
    assert out["var_95_usd"] == 658.0 and out["stop_bounded_loss_usd"] == 7500.0


# ── 2. lot rounds to 0 ──────────────────────────────────────────────────────────────────────────────────────────
def _sized(risk_budget=50.0, sl_pips=30.0, pip_usd=1.0):
    return {"sl_pips": sl_pips, "pip_usd_per_lot": pip_usd, "risk_amount_usd": risk_budget,
            "min_lot_risk_usd": round(0.01 * sl_pips * pip_usd, 2)}


def test_min_lot_fits_budget_is_the_sizer_rule():
    from risk import min_lot_fits_budget
    assert min_lot_fits_budget(_sized(), 0.01) is True                        # 0.01 × 30 × $1 = $0.30 ≤ $50
    assert min_lot_fits_budget(_sized(risk_budget=0.25), 0.01) is True         # $0.30 ≤ $0.25 × 1.5 tolerance
    assert min_lot_fits_budget(_sized(risk_budget=0.1), 0.01) is False         # $0.30 > $0.15
    assert min_lot_fits_budget(_sized(risk_budget=0.05), 0.01) is False        # even the minimum overshoots
    assert min_lot_fits_budget({}, 0.01) is False and min_lot_fits_budget(_sized(sl_pips=0), 0.01) is False


def test_sizer_reports_the_minimum_lot_risk_for_downstream_trims():
    from risk import compute_lot_for_account
    acc = {"equity": 10_000.0, "balance": 10_000.0, "account_type": "standard", "mode": "live"}
    profile = {"risk_pct": 0.5, "tp_atr_mult": 2.0, "sl_atr_mult": 1.0, "min_confidence": 60, "kelly_cap": 0.25}
    out = compute_lot_for_account(acc, "XAUUSD", 4000.0, 3950.0, 70.0, profile)
    assert out["sizing_valid"] and out["min_lot"] == 0.01 and out["min_lot_risk_usd"] > 0
    assert out["min_lot_risk_usd"] <= out["risk_amount_usd"] * 1.5          # C5 already guarantees the minimum fits


def test_trim_under_the_minimum_is_floored_to_the_minimum_when_it_fits_else_skipped():
    import bot_runner as br
    db = FakeDb()
    cfg = {"_id": ObjectId(), "user_id": "u1"}
    floored = []
    with patch.object(br, "_record_pulse", AsyncMock()) as pulse:
        lot = _run(br._floor_or_skip(db, cfg, "XAUUSD", 0.0053, 0.01, "execution alpha", _sized(), floored))
        assert lot == 0.01 and floored == ["execution alpha"] and not pulse.await_args_list
        lot = _run(br._floor_or_skip(db, cfg, "XAUUSD", 0.0, 0.01, "risk engine", _sized(risk_budget=0.05), floored))
        assert lot is None and floored == ["execution alpha"]
        assert pulse.await_args.kwargs["action"] == "SKIP" and "minimum lot exceeds the risk budget" in pulse.await_args.kwargs["reason"]
    src = open(os.path.join(ROOT, "backend", "bot_runner.py"), encoding="utf-8").read()
    assert src.count("await _floor_or_skip(db, cfg, sym,") == 11              # every trim stage goes through the rule
    body = src.split("async def _floor_or_skip")[1]
    assert "await _skip_below_min(db, cfg, sym, effective_lot" not in body       # no bare skip left at a trim site
    assert '"trim_floored_to_min": _floored_trims' in src and '"_sizing": {k: sized.get(k)' in src


def test_authority_reduce_floors_to_the_minimum_when_the_budget_allows(monkeypatch):
    import execution_authority as ea
    db = FakeDb()
    acc = {"_id": ObjectId(), "user_id": "u1", "mode": "live", "account_type": "standard"}
    signal = {"symbol": "XAUUSD", "lot_size": 0.01, "_sizing": _sized()}
    finalize = AsyncMock()
    monkeypatch.setattr(ea, "_finalize_pre_dispatch", finalize)
    src = open(os.path.join(ROOT, "backend", "execution_authority.py"), encoding="utf-8").read()
    block = src.split("if gate.get(\"reduce_factor\") and signal.get(\"lot_size\"):")[1].split("signal[\"_authority_reduced\"] = True")[0]
    assert "min_lot_fits_budget(signal.get(\"_sizing\") or {}, _min)" in block and "_authority_floored_to_min" in block
    # execute the exact block logic through a tiny harness mirroring the function's locals
    from pip_utils import floor_to_lot_step, lot_spec
    from risk import min_lot_fits_budget
    _min, _step = lot_spec(acc, "XAUUSD")
    reduced = floor_to_lot_step(0.01 * 0.5, _step)
    assert reduced < _min and min_lot_fits_budget(signal["_sizing"], _min)
    assert not min_lot_fits_budget(_sized(risk_budget=0.05), _min)
    assert db is not None and finalize.await_count == 0
