"""Risk Commander explicit confirmations (P1 backlog) — deterministic
preview, stored proposals, fingerprint drift refusal, expiry, reject."""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.critical_controls]
from bson import ObjectId
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), ".env"))


def _run(coro):
    from conftest import run_async
    return run_async(coro)


@pytest.fixture
def world(request):
    """Isolated user with 2 bots (one high-risk) and 2 open trades."""
    from database import get_db
    db = get_db()
    uid = str(ObjectId())
    acct = ObjectId()
    _run(db.accounts.insert_one({"_id": acct, "user_id": uid, "display_name": "RC Acct", "bridge_token": f"rc-{acct}"}))
    _run(db.bot_configs.insert_many([
        {"user_id": uid, "account_id": str(acct), "risk_level": "high", "active": True, "active_preset": "scalper"},
        {"user_id": uid, "account_id": None, "risk_level": "low", "active": True, "active_preset": "balanced"},
    ]))
    _run(db.trades.insert_many([
        {"user_id": uid, "symbol": "XAUUSD", "action": "BUY", "status": "open", "entry_price": 4000.0, "live_pnl": 12.5, "lot_size": 0.1},
        {"user_id": uid, "symbol": "BTCUSD", "action": "SELL", "status": "open", "entry_price": 65000.0, "live_pnl": -4.0, "lot_size": 0.01},
    ]))

    def _cleanup():
        for coll in ("bot_configs", "trades", "nl_proposals", "conditional_triggers"):
            _run(db[coll].delete_many({"user_id": uid}))
        _run(db.accounts.delete_one({"_id": acct}))
    request.addfinalizer(_cleanup)
    return {"id": uid, "email": f"rc-{uid}@test.local", "db": db}


def test_preview_enumerates_exact_effects(world):
    from nl_preview import build_preview
    db, uid = world["db"], world["id"]
    actions = [{"type": "DISABLE_BOTS", "target": "high_risk"},
               {"type": "CLOSE_ALL_TRADES", "target": "all"},
               {"type": "SET_RISK_LEVEL", "params": {"risk_level": "low"}},
               {"type": "SET_CONDITIONAL_TRIGGER", "params": {"symbol": "btcusd", "condition": "drop",
                                                              "threshold_pct": 4, "then": [{"type": "DISABLE_BOTS"}]}}]
    p = _run(build_preview(db, uid, actions))
    assert p["capital_touching"] is True
    dis, close, risk, trig = p["actions"]
    assert dis["count"] == 1 and dis["bots"][0]["account"] == "RC Acct" and dis["capital_touching"] is False
    assert close["count"] == 2 and close["live_pnl"] == 8.5 and close["capital_touching"] is True
    assert risk["count"] == 1 and risk["bots"][0]["risk_level"] == "high" and risk["bots"][0]["to"] == "low"
    assert trig["trigger"] == {"symbol": "BTCUSD", "condition": "drop", "threshold_pct": 4.0, "then": ["DISABLE_BOTS"]}
    assert len(p["fingerprint"]) == 64
    # deterministic: same state → same fingerprint
    assert _run(build_preview(db, uid, actions))["fingerprint"] == p["fingerprint"]


def test_confirm_executes_only_matching_preview(world):
    from nl_preview import build_preview, store_proposal
    from routes.nl_routes import nl_command_confirm
    db, uid = world["db"], world["id"]
    user = {"id": uid, "email": world["email"]}
    actions = [{"type": "CLOSE_ALL_TRADES", "target": "all"}]
    doc = _run(store_proposal(db, uid, "close all", actions, _run(build_preview(db, uid, actions))))

    # portfolio drifts before confirm → refused with fresh preview
    _run(db.trades.update_one({"user_id": uid, "symbol": "BTCUSD"}, {"$set": {"status": "closed"}}))
    with pytest.raises(HTTPException) as ei:
        _run(nl_command_confirm({"proposal_id": doc["id"]}, user=user))
    assert ei.value.status_code == 409
    assert ei.value.detail["code"] == "preview_stale"
    assert ei.value.detail["preview"]["actions"][0]["count"] == 1
    assert _run(db.trades.count_documents({"user_id": uid, "close_requested": True})) == 0

    # confirm the refreshed preview → executes
    res = _run(nl_command_confirm({"proposal_id": doc["id"]}, user=user))
    assert res["confirmed"] is True
    assert _run(db.trades.count_documents({"user_id": uid, "close_requested": True})) == 1
    stored = _run(db.nl_proposals.find_one({"_id": ObjectId(doc["id"])}))
    assert stored["status"] == "executed" and stored["receipts"]

    # replay is refused
    with pytest.raises(HTTPException) as ei2:
        _run(nl_command_confirm({"proposal_id": doc["id"]}, user=user))
    assert ei2.value.detail["code"] == "proposal_not_pending"


def test_confirm_refuses_expired_foreign_and_raw(world):
    from nl_preview import build_preview, store_proposal
    from routes.nl_routes import nl_command_confirm
    db, uid = world["db"], world["id"]
    user = {"id": uid, "email": world["email"]}
    actions = [{"type": "DISABLE_BOTS", "target": "all"}]
    doc = _run(store_proposal(db, uid, "disable", actions, _run(build_preview(db, uid, actions))))
    _run(db.nl_proposals.update_one({"_id": ObjectId(doc["id"])}, {"$set": {
        "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}}))
    with pytest.raises(HTTPException) as ei:
        _run(nl_command_confirm({"proposal_id": doc["id"]}, user=user))
    assert ei.value.detail["code"] == "proposal_expired"
    assert _run(db.bot_configs.count_documents({"user_id": uid, "active": True})) == 2

    other = {"id": str(ObjectId()), "email": "x@test.local"}
    doc2 = _run(store_proposal(db, uid, "disable", actions, _run(build_preview(db, uid, actions))))
    with pytest.raises(HTTPException) as ei2:
        _run(nl_command_confirm({"proposal_id": doc2["id"]}, user=other))
    assert ei2.value.status_code == 404

    with pytest.raises(HTTPException) as ei3:
        _run(nl_command_confirm({"actions": actions}, user=user))
    assert ei3.value.detail["code"] == "proposal_id_required"


def test_reject_marks_proposal(world):
    from nl_preview import build_preview, store_proposal
    from routes.nl_routes import nl_command_confirm, nl_command_reject
    db, uid = world["db"], world["id"]
    user = {"id": uid, "email": world["email"]}
    actions = [{"type": "PANIC_LOCK"}]
    doc = _run(store_proposal(db, uid, "panic", actions, _run(build_preview(db, uid, actions))))
    assert _run(nl_command_reject(doc["id"], user=user))["status"] == "rejected"
    with pytest.raises(HTTPException) as ei:
        _run(nl_command_confirm({"proposal_id": doc["id"]}, user=user))
    assert ei.value.detail["code"] == "proposal_not_pending"
    with pytest.raises(HTTPException):
        _run(nl_command_reject(doc["id"], user=user))


def test_nl_command_never_executes_without_confirm(world, monkeypatch):
    import routes.nl_routes as nr
    db, uid = world["db"], world["id"]
    user = {"id": uid, "email": world["email"]}

    async def _fake_interpret(prompt):
        return {"actions": [{"type": "DISABLE_BOTS", "target": "all"}], "summary": "Disable all bots"}

    monkeypatch.setattr(nr, "interpret_command", _fake_interpret)
    res = _run(nr.nl_command({"prompt": "disable everything", "confirm": True}, user=user))
    assert res["requires_confirmation"] is True and res["proposal_id"]
    assert res["preview"]["actions"][0]["count"] == 2
    assert res["sensitive_types"] == []
    assert _run(db.bot_configs.count_documents({"user_id": uid, "active": True})) == 2
