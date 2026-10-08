"""iter247 — setup/teardown helper for the UI RESTART TERMINAL test.
Run with --setup to seed, --teardown to clean up. Prints the created ids on setup.
"""
import hashlib, os, pathlib, secrets, sys
from datetime import datetime, timezone
from pymongo import MongoClient
import requests

ROOT = pathlib.Path("/app")

def _env(path, key):
    for line in (ROOT / path).read_text().splitlines():
        if line.startswith(key + "="):
            v = line.split("=", 1)[1].strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                v = v[1:-1]
            return v

BE = _env("frontend/.env", "REACT_APP_BACKEND_URL").rstrip("/")
MONGO = _env("backend/.env", "MONGO_URL")
DBN = _env("backend/.env", "DB_NAME")
EMAIL = _env("backend/.env", "TEST_ADMIN_EMAIL") or "admin@trading.bot"
PW = _env("backend/.env", "TEST_ADMIN_PASSWORD")

def _hash(t): return hashlib.sha256(t.encode()).hexdigest()

def _login():
    s = requests.Session()
    r = s.post(f"{BE}/api/auth/login", json={"email": EMAIL, "password": PW}, timeout=20)
    r.raise_for_status()
    me = s.get(f"{BE}/api/auth/me", timeout=20).json()
    return s, me["id"]

MARKER = "AGENTID_iter247_ui"

def setup():
    db = MongoClient(MONGO)[DBN]
    s, uid = _login()
    acct = db.accounts.find_one({"user_id": uid, "mode": {"$ne": "paper"},
                                 "account_number": {"$exists": True, "$ne": None}})
    assert acct, "no admin account"
    agent_id = f"agt_iter247ui_{secrets.token_hex(3)}"
    tok = f"agt_tok_{secrets.token_urlsafe(32)}"
    now = datetime.now(timezone.utc)
    db.vps_agents.insert_one({
        "agent_id": agent_id, "user_id": uid, "agent_token_hash": _hash(tok),
        "revoked": False, "command_seq": 0,
        "last_heartbeat": now, "registered_at": now, "deployment_id": "dep_ui_iter247",
        "_marker": MARKER,
    })
    r = requests.post(f"{BE}/api/vps/agent/terminals/report", json={
        "agent_token": tok, "login": str(acct["account_number"]),
        "account_id": str(acct["_id"]), "status": "running",
        "detail": "iter247 UI fixture",
    }, timeout=20)
    r.raise_for_status()
    print(f"ACCOUNT_ID={acct['_id']}")
    print(f"AGENT_ID={agent_id}")
    # Persist to a file for teardown
    (ROOT / "test_reports" / "iter247_ui_state.txt").write_text(
        f"{agent_id}\n{acct['_id']}\n")

def teardown():
    db = MongoClient(MONGO)[DBN]
    f = ROOT / "test_reports" / "iter247_ui_state.txt"
    if not f.exists():
        print("nothing to teardown"); return
    agent_id, acct_id = f.read_text().splitlines()
    from bson import ObjectId
    db.vps_agents.delete_many({"agent_id": agent_id})
    db.mt5_instances.delete_many({"agent_id": agent_id})
    db.agent_commands.delete_many({"agent_id": agent_id})
    db.accounts.update_one({"_id": ObjectId(acct_id)}, {"$unset": {"vps_terminal": ""}})
    f.unlink()
    print("teardown ok")

if __name__ == "__main__":
    (setup if sys.argv[1] == "--setup" else teardown)()
