"""Security audit r27 — a live account must not self-label DEMO to bypass the
EX5 binary-proof gate. DEMO is server-authoritative: admin attestation that
agrees with the declared classification (broker_env.attested_environment)."""
import os
import sys

import pytest
from datetime import datetime, timezone
from bson import ObjectId
from fastapi import HTTPException

pytestmark = [pytest.mark.integration, pytest.mark.critical_controls]

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), ".env"))


def _run(coro):
    from conftest import run_async
    return run_async(coro)


@pytest.fixture
def world(request):
    from database import get_db
    db = get_db()
    uid = str(ObjectId())
    admin = {"id": str(ObjectId()), "email": f"adm-{uid[-6:]}@example.com", "role": "admin", "two_factor_enabled": True}

    def _cleanup():
        ids = [str(a["_id"]) for a in _run(db.accounts.find({"user_id": uid}).to_list(10))]
        _run(db.accounts.delete_many({"user_id": uid}))
        _run(db.admin_audit_log.delete_many({"target_id": {"$in": ids}}))
    request.addfinalizer(_cleanup)
    acc_id = _run(db.accounts.insert_one({"user_id": uid, "label": "r27", "mode": "live", "account_type": "demo",
                                          "broker": "RoboForex", "server": "RoboForex-Demo", "bridge_token": f"r27-{uid}",
                                          "account_number": "777", "ea_version": "1.58",
                                          "ea_identity": {"installation_id": "inst-A", "authoritative": True, "broker_server": "RoboForex-Demo"},
                                          "broker_account_id_reported": "777", "last_heartbeat": datetime.now(timezone.utc).isoformat(),})).inserted_id
    live_id = _run(db.accounts.insert_one({"user_id": uid, "label": "r27-live", "mode": "live",
                                           "account_type": "standard", "broker": "RoboForex", "bridge_token": f"r27l-{uid}",
                                           "server": "RoboForex-ECN", "account_number": "778",
                                           "ea_version": "1.58"})).inserted_id
    return {"db": db, "acc_id": acc_id, "live_id": live_id, "admin": admin}


def test_declared_demo_is_live_until_attested(world):
    from broker_env import attested_environment, broker_environment
    from ea_capabilities import live_gate
    acc = _run(world["db"].accounts.find_one({"_id": world["acc_id"]}))
    assert broker_environment(acc) == "DEMO" and attested_environment(acc) == "LIVE"
    assert live_gate(acc)["code"] == "EA_DEMO_UNATTESTED"


def test_admin_attestation_requires_reauth_and_declared_demo(world, monkeypatch):
    import routes.admin_routes as ar
    from broker_env import attested_environment
    from ea_capabilities import live_gate
    db, admin = world["db"], world["admin"]

    async def _bad(*a, **k):
        raise HTTPException(status_code=401, detail={"code": "reauth_failed"})
    monkeypatch.setattr(ar, "_reauth", _bad)
    with pytest.raises(HTTPException) as ei:
        _run(ar.admin_attest_account_environment(str(world["acc_id"]), {"environment": "DEMO", "password": "x"}, admin))
    assert ei.value.status_code == 401
    assert attested_environment(_run(db.accounts.find_one({"_id": world["acc_id"]}))) == "LIVE"

    async def _ok(*a, **k):
        return None
    monkeypatch.setattr(ar, "_reauth", _ok)
    # a LIVE-declared account can never be attested DEMO
    with pytest.raises(HTTPException) as ei:
        _run(ar.admin_attest_account_environment(str(world["live_id"]), {"environment": "DEMO", "password": "x"}, admin))
    assert ei.value.status_code == 409
    # non-admin refused
    with pytest.raises(HTTPException) as ei:
        _run(ar.admin_attest_account_environment(str(world["acc_id"]), {"environment": "DEMO", "password": "x"},
                                                 {**admin, "role": "user"}))
    assert ei.value.status_code == 403

    row = _run(ar.admin_attest_account_environment(str(world["acc_id"]), {"environment": "DEMO", "password": "x",
                                                                           "reason": "practice"}, admin))
    assert row["effective"] == "DEMO" and row["attested_by"] == admin["email"]
    acc = _run(db.accounts.find_one({"_id": world["acc_id"]}))
    assert live_gate(acc) is None                                   # unverified local EX5 may trade practice money
    audit = _run(db.admin_audit_log.find_one({"target_id": str(world["acc_id"]), "action": "account_environment_attest"}))
    assert audit and audit["meta"]["environment"] == "DEMO" and audit["meta"]["reauth"] is True

    # attestation is inert if the owner later makes the record look live (defence in depth)
    _run(db.accounts.update_one({"_id": world["acc_id"]}, {"$set": {"broker_environment": "LIVE"}}))
    assert live_gate(_run(db.accounts.find_one({"_id": world["acc_id"]})))["code"] in ("EA_BINARY_PROOF_MISSING", "EA_RELEASE_HASH_UNPINNED")
    _run(db.accounts.update_one({"_id": world["acc_id"]}, {"$unset": {"broker_environment": ""}}))

    row = _run(ar.admin_attest_account_environment(str(world["acc_id"]), {"environment": "LIVE", "password": "x"}, admin))
    assert row["effective"] == "LIVE" and row["attested_by"] is None
    listing = _run(ar.admin_account_environments(admin))
    mine = {r["account_id"]: r for r in listing["accounts"]}
    assert mine[str(world["acc_id"])]["declared"] == "DEMO" and str(world["live_id"]) not in mine   # only actionable rows
