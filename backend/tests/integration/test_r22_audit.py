"""Audit r22 hardenings — one-time e-mail tokens hashed at rest, TOTP used-code
cache, user_trust binary hash labelled unattested, per-IP throttles on the
unauthenticated pairing/bridge surface."""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId
from fastapi import HTTPException

pytestmark = [pytest.mark.integration, pytest.mark.critical_controls]

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(ROOT, "backend"))
from dotenv import load_dotenv
load_dotenv(os.path.join(ROOT, "backend", ".env"))

PINNED = "c" * 64


def _run(coro):
    from conftest import run_async
    return run_async(coro)


def _request(ip: str):
    from fastapi import Request
    return Request({"type": "http", "method": "POST", "path": "/x", "headers": [], "query_string": b"",
                    "client": (ip, 40000), "server": ("api", 443), "scheme": "https"})


@pytest.fixture
def user(request):
    from database import get_db
    db = get_db()
    email = f"r22-{uuid.uuid4().hex[:10]}@example.com"
    uid = _run(db.users.insert_one({"email": email, "password_hash": "x", "email_verified": True,
                                    "name": "r22"})).inserted_id
    request.addfinalizer(lambda: (_run(db.users.delete_one({"_id": uid})),
                                  _run(db.totp_used.delete_many({"_id": {"$regex": f"^{uid}:"}}))))
    return {"db": db, "uid": uid, "email": email}


def test_reset_token_stored_as_digest_and_plaintext_never_redeems(user):
    from models import ResetPasswordRequest
    from routes.auth_routes import reset_password
    from security import token_digest
    db, uid = user["db"], user["uid"]
    raw = "r22-reset-" + uuid.uuid4().hex
    exp = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    # a leaked plaintext column (legacy shape) is never a valid credential any more
    _run(db.users.update_one({"_id": uid}, {"$set": {"password_reset_token": raw, "password_reset_expires_at": exp}}))
    with pytest.raises(HTTPException) as e:
        _run(reset_password(ResetPasswordRequest(token=raw, new_password=f"Zq7!{uuid.uuid4().hex[:18]}vX")))
    assert e.value.status_code == 400
    _run(db.users.update_one({"_id": uid}, {"$set": {"password_reset_token_sha256": token_digest(raw)},
                                            "$unset": {"password_reset_token": ""}}))
    _run(reset_password(ResetPasswordRequest(token=raw, new_password=f"Zq7!{uuid.uuid4().hex[:18]}vX")))
    u = _run(db.users.find_one({"_id": uid}))
    assert "password_reset_token_sha256" not in u and "password_reset_token" not in u     # single use
    src = open(os.path.join(ROOT, "backend", "routes", "auth_routes.py")).read()
    assert '"password_reset_token": token' not in src and '"activation_token": token' not in src
    assert src.count("token_digest(") >= 5                                                 # both flows, store + lookup


def test_totp_code_cannot_be_redeemed_twice(user):
    import pyotp
    from totp import new_secret, verify_code_once
    secret = new_secret()
    code = pyotp.TOTP(secret).now()
    assert _run(verify_code_once(user["db"], str(user["uid"]), secret, code)) is True
    assert _run(verify_code_once(user["db"], str(user["uid"]), secret, code)) is False
    assert _run(verify_code_once(user["db"], str(user["uid"]), secret, "000000")) is False
    src = open(os.path.join(ROOT, "backend", "routes", "auth_routes.py")).read()
    assert src.count("verify_code_once(") == 3       # login 2FA, disable, step-up


def test_user_trust_binary_hash_is_labelled_unattested(monkeypatch):
    monkeypatch.setenv("EA_RELEASE_SHA256", PINNED)
    from database import get_db
    from ea_capabilities import live_gate
    from models import BridgeHeartbeat
    from routes.bridge_routes import heartbeat
    db = get_db()
    uid, token = str(ObjectId()), f"r22-{uuid.uuid4().hex}"
    acc_id = _run(db.accounts.insert_one({"user_id": uid, "status": "active", "trading_enabled": True, "mode": "live",
                                          "bridge_token": token, "account_number": "555002",
                                          "broker_server": "Demo-Server"})).inserted_id
    inst = f"trust-{uuid.uuid4().hex[:12]}"
    _run(db.installations.insert_one({"installation_id": inst, "account_id": str(acc_id), "method": "user_trust",
                                      "trusted_fingerprint": {"account_login": "555002", "broker_server": "Demo-Server"},
                                      "revoked": False}))
    _run(db.execution_leases.insert_one({"account_id": str(acc_id), "installation_id": inst, "revoked": False}))
    try:
        _run(heartbeat(BridgeHeartbeat(bridge_token=token, balance=1.0, equity=1.0, account_login=555002,
                                       broker_server="Demo-Server", ea_version="1.57", client_version="1.57",
                                       ea_binary_sha256=PINNED)))
        acc = _run(db.accounts.find_one({"_id": acc_id}))
        assert acc["ea_identity"]["authoritative"] is True
        assert acc["ea_binary_sha256"] == PINNED and acc["ea_binary_sha256_method"] == "user_trust"
        assert live_gate(acc)["code"] == "EA_BINARY_PROOF_UNATTESTED"
        assert live_gate({**acc, "ea_binary_sha256_method": "installer"}) is None
    finally:
        _run(db.accounts.delete_many({"user_id": uid}))
        _run(db.installations.delete_many({"account_id": str(acc_id)}))
        _run(db.execution_leases.delete_many({"account_id": str(acc_id)}))


def test_claim_pairing_and_bridge_surface_are_throttled_per_ip(monkeypatch):
    import routes.bridge_routes as br
    from routes.setup_routes import ClaimPairingRequest, claim_pairing_token
    ip = f"203.0.113.{uuid.uuid4().int % 250 + 1}"
    codes = []
    for _ in range(21):
        with pytest.raises(HTTPException) as e:
            _run(claim_pairing_token(ClaimPairingRequest(token="not-a-real-token"), _request(ip)))
        codes.append(e.value.status_code)
    assert codes[:20] == [400] * 20 and codes[20] == 429
    monkeypatch.setattr(br, "BRIDGE_IP_LIMIT_PER_MIN", 3)
    ip2 = f"198.51.100.{uuid.uuid4().int % 250 + 1}"
    for _ in range(3):
        _run(br._bridge_ip_throttle(_request(ip2)))
    with pytest.raises(HTTPException) as e:
        _run(br._bridge_ip_throttle(_request(ip2)))
    assert e.value.status_code == 429
    assert "dependencies=[Depends(_bridge_ip_throttle)]" in open(br.__file__).read()
