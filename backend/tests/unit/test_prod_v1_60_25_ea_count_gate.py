"""prod v1.60.25 — `ea_count_agrees_with_worker_view` compared contract().totals.eas_connected (ALL accounts, disabled
ones with a live EA included) against ea_fresh over ENABLED accounts only, so any disabled account with a running EA
failed every production update. Both sides now count enabled accounts only."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "ops"))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _wrow(enabled, connected):
    return {"account_enabled": enabled, "ea_connected": connected}


def test_disabled_account_with_live_ea_no_longer_fails_the_gate():
    import prepromotion_evidence as pp
    worker_rows = [_wrow(True, True), _wrow(False, True)]                      # the OFF account still runs an EA
    en_rows = [{"enabled": True, "ea_fresh": True}]
    totals = {"eas_connected": 2, "accounts_enabled": 1}
    # the old comparison (totals over ALL accounts) is exactly the prod failure
    assert totals["eas_connected"] != sum(1 for x in en_rows if x["ea_fresh"])
    view = pp.worker_enabled_view(worker_rows, totals=totals)
    assert view == {"accounts_total": 2, "accounts_enabled": 1, "eas_connected": 1, "eas_connected_all_accounts": 2}
    assert pp.ea_count_gate(view, en_rows) is True


def test_gate_still_catches_real_disagreement_on_enabled_accounts():
    import prepromotion_evidence as pp
    # worker sees the enabled EA as disconnected while evidence says fresh → FAIL
    assert pp.ea_count_gate(pp.worker_enabled_view([_wrow(True, False)]), [{"enabled": True, "ea_fresh": True}]) is False
    # enabled-count mismatch → FAIL
    assert pp.ea_count_gate(pp.worker_enabled_view([_wrow(True, True), _wrow(True, True)]), [{"enabled": True, "ea_fresh": True}]) is False
    # two enabled, both agree → PASS regardless of OFF accounts
    assert pp.ea_count_gate(pp.worker_enabled_view([_wrow(True, True), _wrow(True, True), _wrow(False, True), _wrow(False, False)]),
                            [{"enabled": True, "ea_fresh": True}, {"enabled": True, "ea_fresh": True}]) is True


def test_contract_rows_feed_the_enabled_scoped_view():
    import prepromotion_evidence as pp
    from state_contract import contract
    db = FakeDb()
    now = datetime.now(timezone.utc)
    _run(db.accounts.insert_one({"_id": ObjectId(), "user_id": "u1", "label": "ON", "mode": "live", "trading_enabled": True,
                                 "last_heartbeat": now.isoformat(), "open_positions": 0}))
    _run(db.accounts.insert_one({"_id": ObjectId(), "user_id": "u1", "label": "OFF with EA", "mode": "live", "trading_enabled": False,
                                 "last_heartbeat": now.isoformat(), "open_positions": 0}))
    _run(db.accounts.insert_one({"_id": ObjectId(), "user_id": "u1", "label": "OFF stale", "mode": "live", "trading_enabled": False,
                                 "last_heartbeat": (now - timedelta(hours=3)).isoformat()}))
    ctr = _run(contract(db, "u1"))
    assert ctr["totals"]["eas_connected"] == 2 and ctr["totals"]["accounts_enabled"] == 1      # the inflated worker total
    view = pp.worker_enabled_view(ctr["accounts"], totals=ctr["totals"])
    assert view["eas_connected"] == 1 and view["accounts_enabled"] == 1 and view["eas_connected_all_accounts"] == 2
    assert pp.ea_count_gate(view, [{"enabled": True, "ea_fresh": True}]) is True


def test_evidence_source_uses_the_enabled_scoped_view():
    src = open(os.path.join(ROOT, "backend", "ops", "prepromotion_evidence.py"), encoding="utf-8").read()
    assert 'worker_view = worker_enabled_view(ctr.get("accounts") or [], totals=ctr.get("totals") or {})' in src
    assert 'gates["ea_count_agrees_with_worker_view"] = ea_count_gate(worker_view, en_rows)' in src
    assert 'worker_view.get("eas_connected") == sum(1 for x in en_rows if x["ea_fresh"])' in src.split("def ea_count_gate")[1].split("def environment_gates")[0]
    assert '"worker_view": worker_view' in src                                        # the bundle still records the worker side
