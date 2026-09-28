"""Audit r21 — SEC-001 EA binary proof admitted only over a verified installation
chain (token-only echo of the public release hash never unlocks live);
hardening: no stray Access-Control-Allow-Credentials without a matched origin."""
import os
import sys
import uuid

import pytest
from bson import ObjectId

pytestmark = [pytest.mark.integration, pytest.mark.critical_controls]

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(ROOT, "backend"))
from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT, "backend", ".env"))

PINNED = "c" * 64


def _run(coro):
    from conftest import run_async
    return run_async(coro)


@pytest.fixture
def world(request):
    from database import get_db
    db = get_db()
    uid = str(ObjectId())
    token = f"r21-{uuid.uuid4().hex}"
    acc_id = _run(db.accounts.insert_one({
        "user_id": uid, "status": "active", "trading_enabled": True, "mode": "live",
        "bridge_token": token, "account_number": "555001", "broker_server": "Demo-Server",
        "name": "r21"})).inserted_id

    def _cleanup():
        _run(db.accounts.delete_many({"user_id": uid}))
        _run(db.installations.delete_many({"account_id": str(acc_id)}))
        _run(db.execution_leases.delete_many({"account_id": str(acc_id)}))
    request.addfinalizer(_cleanup)
    return {"db": db, "uid": uid, "acc_id": acc_id, "token": token}


def _hb(world, **extra):
    from models import BridgeHeartbeat
    from routes.bridge_routes import heartbeat
    payload = BridgeHeartbeat(bridge_token=world["token"], balance=1000.0, equity=1000.0,
                              account_login=555001, broker_server="Demo-Server", **extra)
    _run(heartbeat(payload))
    return _run(world["db"].accounts.find_one({"_id": world["acc_id"]}))


def test_token_only_heartbeat_echoing_pinned_hash_never_unlocks_live(world, monkeypatch):
    monkeypatch.setenv("EA_RELEASE_SHA256", PINNED)
    from ea_capabilities import live_gate
    acc = _hb(world, ea_version="1.57", client_version="1.57", ea_binary_sha256=PINNED)
    assert acc.get("ea_binary_sha256") is None                      # not admitted as proof
    assert acc["ea_binary_sha256_reported"] == PINNED               # recorded as unverified telemetry
    assert acc["ea_identity"]["authoritative"] is False
    assert live_gate(acc)["code"] == "EA_BINARY_PROOF_MISSING"


def test_verified_chain_admits_proof_and_unverified_takeover_revokes_it(world, monkeypatch):
    monkeypatch.setenv("EA_RELEASE_SHA256", PINNED)
    from ea_capabilities import live_gate
    db, acc_id = world["db"], str(world["acc_id"])
    inst = f"inst-{uuid.uuid4().hex[:12]}"
    _run(db.installations.insert_one({"installation_id": inst, "account_id": acc_id, "method": "installer",
                                      "broker_server": "Demo-Server", "revoked": False}))
    _run(db.execution_leases.insert_one({"account_id": acc_id, "installation_id": inst, "revoked": False}))
    acc = _hb(world, installation_id=inst, ea_version="1.57", client_version="1.57", ea_binary_sha256=PINNED)
    assert acc["ea_identity"]["authoritative"] is True, acc["ea_identity"]
    assert acc["ea_binary_sha256"] == PINNED
    assert live_gate(acc) is None
    # a token-only terminal (no chain) taking over the token loses the admitted proof
    acc = _hb(world, ea_version="1.57", client_version="1.57", ea_binary_sha256=PINNED)
    assert acc.get("ea_binary_sha256") is None
    assert live_gate(acc)["code"] == "EA_BINARY_PROOF_MISSING"


def test_malformed_hash_is_ignored(world):
    acc = _hb(world, ea_version="1.57", ea_binary_sha256="Z" * 64)
    assert "ea_binary_sha256_reported" not in acc and acc.get("ea_binary_sha256") is None


def test_no_stray_allow_credentials_without_matched_origin():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from starlette.middleware.cors import CORSMiddleware
    from security import StripStrayCorsCredentials
    mini = FastAPI()

    @mini.get("/ping")
    def ping():
        return {"ok": True}

    mini.add_middleware(CORSMiddleware, allow_origins=["https://app.example"], allow_credentials=True,
                        allow_methods=["*"], allow_headers=["*"])
    mini.add_middleware(StripStrayCorsCredentials)
    c = TestClient(mini)
    foreign = c.get("/ping", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in foreign.headers
    assert "access-control-allow-credentials" not in foreign.headers
    ok = c.get("/ping", headers={"Origin": "https://app.example"})
    assert ok.headers["access-control-allow-origin"] == "https://app.example"
    assert ok.headers["access-control-allow-credentials"] == "true"
    server_src = open(os.path.join(ROOT, "backend", "server.py")).read()
    assert "app.add_middleware(_StripStrayCorsCredentials)" in server_src
