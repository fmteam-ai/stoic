"""Audit r20 — P1-01 mandatory EA binary proof pinned from the release record,
P1-02 capital-aware transaction predicate, P2-01/02 unified close protocol +
immutable command ledger, P2-03 leased PANIC outbox, P2-04 fingerprint inputs,
P2-05 admission commit binding."""
import os
import sys
from datetime import datetime, timezone

import pytest
from bson import ObjectId

pytestmark = [pytest.mark.integration, pytest.mark.critical_controls]

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(ROOT, "backend"))
from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT, "backend", ".env"))


def _run(coro):
    from conftest import run_async
    return run_async(coro)


@pytest.fixture
def world(request):
    from database import get_db
    db = get_db()
    uid = str(ObjectId())

    def _cleanup():
        for c in ("trades", "accounts", "close_commands"):
            _run(db[c].delete_many({"user_id": uid}))
        _run(db.close_commands.delete_many({"actor": {"$regex": uid}}))
    request.addfinalizer(_cleanup)
    return {"id": uid, "db": db}


def test_request_close_allocates_sequence_ledger_and_supersession(world):
    from close_commands import request_close, acknowledge_close, cancel_pending
    db, uid = world["db"], world["id"]
    t_open = _run(db.trades.insert_one({"user_id": uid, "symbol": "XAUUSD", "status": "open", "mt5_ticket": 7})).inserted_id
    t_pend = _run(db.trades.insert_one({"user_id": uid, "symbol": "XAUUSD", "status": "pending"})).inserted_id
    a = _run(request_close(db, {"user_id": uid}, reason="protection_guard", actor=f"guard:{uid}"))
    assert a["trades_marked_for_close"] == 1 and a["trade_ids"] == [str(t_open)]      # pending never position-closed
    b = _run(request_close(db, {"user_id": uid}, reason="nl_command", actor=f"risk_commander:{uid}",
                           ctx={"idempotency_key": "k" * 32, "fence": 3, "execution_id": "ex9"}))
    t = _run(db.trades.find_one({"_id": t_open}))
    assert t["close_seq"] == 2 and t["close_idem_key"] == "k" * 32 and t["close_command"]["actor"].startswith("risk_commander")
    rows = _run(db.close_commands.find({"trade_id": str(t_open)}).sort("close_seq", 1).to_list(10))
    assert [r["close_seq"] for r in rows] == [1, 2]
    assert rows[0]["state"] == "superseded" and rows[0]["superseded_by"] == "k" * 32
    assert rows[1]["supersedes"] == a["command_id"] and rows[1]["fence"] == 3 and rows[1]["execution_id"] == "ex9"
    ack = _run(acknowledge_close(db, _run(db.trades.find_one({"_id": t_open})), broker_deal_id="D1", occurred_at="2026-06-01T00:00:00+00:00"))
    assert ack["state"] == "broker_confirmed" and ack["close_seq"] == 2
    assert _run(db.close_commands.find_one({"_id": f"{'k' * 32}:{t_open}"}))["broker_result"]["deal_id"] == "D1"
    assert _run(cancel_pending(db, {"user_id": uid}, reason="nl_command", actor="x")) == 1
    assert _run(db.trades.find_one({"_id": t_pend}))["status"] == "cancelled"


def test_capital_capable_treats_missing_or_non_paper_mode_and_bindings_as_live(world, monkeypatch):
    import nl_execution as nx
    db, uid = world["db"], world["id"]
    baseline = _run(nx.capital_capable(db))
    if baseline:
        pytest.skip("shared preview database already holds capital-capable accounts")
    for doc in ({"trading_enabled": True}, {"trading_enabled": True, "mode": ""}, {"trading_enabled": True, "mode": "LIVE"},
                {"trading_enabled": True, "mode": "demo"}, {"bridge_token": "tok"}, {"last_heartbeat": "2026-01-01T00:00:00+00:00"}):
        _run(db.accounts.insert_one({"user_id": uid, "status": "active", **doc}))
        assert _run(nx.capital_capable(db)) is True, doc
        _run(db.accounts.delete_many({"user_id": uid}))
    _run(db.accounts.insert_one({"user_id": uid, "status": "active", "trading_enabled": True, "mode": "Paper"}))
    assert _run(nx.capital_capable(db)) is False
    _run(db.accounts.delete_many({"user_id": uid}))


def test_heartbeat_reports_binary_hash_and_fingerprint_tracks_ea_inputs(world):
    from models import BridgeHeartbeat  # noqa: F401  (schema carries ea_binary_sha256)
    import models
    assert "ea_binary_sha256" in open(models.__file__).read()
    br = open(os.path.join(ROOT, "backend", "routes", "bridge_routes.py")).read()
    assert 'set_doc["ea_binary_sha256"] = reported_hash' in br
    from canonical_decision import inventory_fingerprint
    db, uid = world["db"], world["id"]
    acc = _run(db.accounts.insert_one({"user_id": uid, "trading_enabled": True, "mode": "live", "ea_version": "1.56",
                                       "status": "active"})).inserted_id
    f1 = _run(inventory_fingerprint(db, uid))
    _run(db.accounts.update_one({"_id": acc}, {"$set": {"ea_version": "1.58"}}))
    f2 = _run(inventory_fingerprint(db, uid))
    _run(db.accounts.update_one({"_id": acc}, {"$set": {"ea_binary_sha256": "c" * 64}}))
    f3 = _run(inventory_fingerprint(db, uid))
    _run(db.accounts.update_one({"_id": acc}, {"$set": {"last_heartbeat": datetime.now(timezone.utc).isoformat()}}))
    f4 = _run(inventory_fingerprint(db, uid))
    assert len({f1, f2, f3, f4}) == 4                       # version, hash and heartbeat freshness all invalidate the cache


def test_admission_commit_binding_and_attestation_hash():
    lib = open(os.path.join(ROOT, "deploy", "lib.sh")).read()
    assert '--tag "${tag}" --commit "${GIT_SHA}"' in lib
    rel = open(os.path.join(ROOT, ".github", "workflows", "release.yml")).read()
    assert "release_attestation.py emit --admission release-admission.json" in rel
    att = open(os.path.join(ROOT, "scripts", "release_attestation.py")).read()
    assert '"release_admission_sha256"' in att
