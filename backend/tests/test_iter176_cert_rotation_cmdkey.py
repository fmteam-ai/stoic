"""iter-176 — command_key encrypted at rest + mTLS cert rotation policy.
"""
import hashlib
import hmac as _hmac
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import requests

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)
from dotenv import load_dotenv
load_dotenv(os.path.join(_BACKEND_DIR, ".env"))

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
API = f"{BASE_URL}/api"
ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PW = "admin123"
TIMEOUT = 25


def _run(coro):
    from conftest import run_async
    return run_async(coro)


def _db():
    from database import get_db
    return get_db()


def _admin():
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PW},
               timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return s


def _now():
    return datetime.now(timezone.utc)


# ─── command_key at rest ─────────────────────────────────────────────
def test_command_key_encrypted_at_rest_and_signs_commands():
    from vps_agent import agent_command_key, encrypt_command_key
    from vps_pathb import queue_command
    db = _db()
    uid = f"iter176-{uuid.uuid4().hex[:8]}"
    agent_id = f"agt_test_{uuid.uuid4().hex[:8]}"
    plain_key = uuid.uuid4().hex
    _run(db.vps_agents.insert_one({
        "agent_id": agent_id, "user_id": uid,
        "command_key_enc": encrypt_command_key(plain_key),
        "command_seq": 0, "last_acked_seq": 0, "revoked": False,
        "chaos": True}))
    try:
        doc = _run(db.vps_agents.find_one({"agent_id": agent_id}))
        assert "command_key" not in doc          # no plaintext at rest
        assert doc["command_key_enc"]["ciphertext"] != plain_key
        assert agent_command_key(doc) == plain_key

        cmd = _run(queue_command(db, uid, agent_id, "run_diagnostics",
                                 None, "iter176-test"))
        stored = _run(db.agent_commands.find_one(
            {"command_id": cmd["command_id"]}))
        expect = _hmac.new(
            plain_key.encode(),
            f"{agent_id}|{cmd['command_id']}|{stored['seq']}|run_diagnostics".encode(),
            hashlib.sha256).hexdigest()
        assert stored["sig"] == expect           # signed with decrypted key
    finally:
        _run(db.vps_agents.delete_one({"agent_id": agent_id}))
        _run(db.agent_commands.delete_many({"agent_id": agent_id}))


def test_command_key_legacy_plaintext_fallback():
    from vps_agent import agent_command_key
    assert agent_command_key({"command_key": "legacy"}) == "legacy"
    assert agent_command_key({}) is None


def test_register_agent_stores_encrypted_command_key():
    from vps_agent import create_bootstrap_token, register_agent
    db = _db()
    uid = f"iter176-reg-{uuid.uuid4().hex[:8]}"
    boot = _run(create_bootstrap_token(db, uid, f"dep-{uuid.uuid4().hex[:6]}"))
    out = _run(register_agent(db, boot["token"],
                              {"machine_fingerprint": "fp-test"}))
    try:
        assert out["command_key"]                # plaintext returned ONCE
        doc = _run(db.vps_agents.find_one({"agent_id": out["agent_id"]}))
        assert "command_key" not in doc
        from vps_agent import agent_command_key
        assert agent_command_key(doc) == out["command_key"]
    finally:
        _run(db.vps_agents.delete_one({"agent_id": out["agent_id"]}))


def test_rotate_credentials_stores_encrypted_key():
    db = _db()
    uid = f"iter176-rot-{uuid.uuid4().hex[:8]}"
    agent_id = f"agt_rot_{uuid.uuid4().hex[:8]}"
    admin = _run(db.users.find_one({"email": ADMIN_EMAIL}))
    _run(db.vps_agents.insert_one({
        "agent_id": agent_id, "user_id": str(admin["_id"]),
        "command_key": "old-plaintext", "revoked": False, "chaos": True}))
    try:
        s = _admin()
        r = s.post(f"{API}/infra/agents/{agent_id}/rotate-credentials",
                   timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["command_key"] != "old-plaintext"
        doc = _run(db.vps_agents.find_one({"agent_id": agent_id}))
        assert "command_key" not in doc          # plaintext dropped
        from vps_agent import agent_command_key
        assert agent_command_key(doc) == body["command_key"]
    finally:
        _run(db.vps_agents.delete_one({"agent_id": agent_id}))


# ─── cert rotation policy ────────────────────────────────────────────
def _mk_cert_agent(db, agent_id, not_after):
    _run(db.vps_agents.insert_one({
        "agent_id": agent_id, "user_id": "iter176-cert", "revoked": False,
        "chaos": True,
        "mtls": {"fingerprint": "ff" * 32, "revoked": False,
                 "not_after": not_after.isoformat()}}))


def test_certs_expiring_buckets_and_alert():
    from agent_mtls import RENEW_WINDOW_DAYS, certs_expiring, check_cert_expiry
    db = _db()
    a_exp = f"agt_cexp_{uuid.uuid4().hex[:8]}"
    a_soon = f"agt_csoon_{uuid.uuid4().hex[:8]}"
    a_fresh = f"agt_cok_{uuid.uuid4().hex[:8]}"
    _mk_cert_agent(db, a_exp, _now() - timedelta(days=1))
    _mk_cert_agent(db, a_soon, _now() + timedelta(days=RENEW_WINDOW_DAYS - 1))
    _mk_cert_agent(db, a_fresh, _now() + timedelta(days=RENEW_WINDOW_DAYS + 30))
    try:
        posture = _run(certs_expiring(db))
        exp_ids = {i["agent_id"] for i in posture["expired"]}
        soon_ids = {i["agent_id"] for i in posture["expiring_soon"]}
        assert a_exp in exp_ids
        assert a_soon in soon_ids
        assert a_fresh not in exp_ids | soon_ids

        _run(db.ops_alerts.delete_many({"kind": "agent_cert_rotation_due"}))
        _run(check_cert_expiry(db))
        alert = _run(db.ops_alerts.find_one(
            {"kind": "agent_cert_rotation_due", "acked_at": None}))
        assert alert and alert["severity"] == "critical"  # expired present
    finally:
        _run(db.vps_agents.delete_many(
            {"agent_id": {"$in": [a_exp, a_soon, a_fresh]}}))
        _run(db.ops_alerts.delete_many({"kind": "agent_cert_rotation_due"}))


def test_ops_agent_certs_endpoint_admin_only():
    r = requests.get(f"{API}/ops/agent-certs", timeout=TIMEOUT)
    assert r.status_code == 403
    s = _admin()
    r = s.get(f"{API}/ops/agent-certs", timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    body = r.json()
    assert {"renew_window_days", "expired", "expiring_soon"} <= set(body)
