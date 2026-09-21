"""Shared test helpers — iter-143 legacy-suite refresh.

Registration now requires terms acceptance AND email verification before
login (iter-79 activation flow). Legacy suites written before that assumed
register == logged-in session. This helper does the full modern flow:
register → mark verified straight in Mongo → login → cookie session.
"""
import os

import requests
from dotenv import load_dotenv
from pymongo import MongoClient

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BACKEND, ".env"))


def base_url() -> str:
    """Shared live-target resolution (RC review P1) — SKIPS the calling
    suite when no live deployment exists instead of erroring collection."""
    from live_target import require_live_base_url
    return require_live_base_url()


def mongo_db():
    return MongoClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]


def mark_email_verified(email: str) -> None:
    mongo_db().users.update_one(
        {"email": email.lower()},
        {"$set": {"email_verified": True},
         "$unset": {"activation_token": "", "activation_expires_at": ""}})


def make_elite(email: str) -> None:
    """Give the user an active Elite subscription (unlimited accounts) so
    tests can exercise gates that sit BEHIND the tier quota."""
    from datetime import datetime, timedelta, timezone
    valid = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    db = mongo_db()
    u = db.users.find_one({"email": email.lower()})
    assert u, f"no such user {email}"
    db.subscriptions.update_one(
        {"user_id": str(u["_id"])},
        {"$set": {"current_plan_id": "elite_ai_monthly", "valid_until": valid}},
        upsert=True)


def register_and_login(email: str, password: str = "Kd5#Zt9mW2xVpR7c",
                       name: str = "QA") -> requests.Session:
    api = f"{base_url()}/api"
    s = requests.Session()
    r = s.post(f"{api}/auth/register",
               json={"email": email, "password": password, "name": name,
                     "terms_agreed": True}, timeout=30)
    assert r.status_code == 200, f"register failed: {r.status_code} {r.text}"
    mark_email_verified(email)
    r = s.post(f"{api}/auth/login",
               json={"email": email, "password": password}, timeout=30)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return s


def seed_attestation_eligible_user(email: str) -> requests.Session:
    """Audit P1-2 — a user whose dataset PASSES the hardened attestation
    gate: one LIVE-classified, enabled account with a fresh heartbeat and
    zero open positions, plus one fresh broker deal. Returns a logged-in
    session. Clean up with `cleanup_attestation_user(email)`."""
    import time
    from datetime import datetime, timezone
    from bson import ObjectId
    s = register_and_login(email)
    db = mongo_db()
    uid = str(db.users.find_one({"email": email.lower()})["_id"])
    now = datetime.now(timezone.utc)
    acc_id = ObjectId()
    db.accounts.insert_one({
        "_id": acc_id, "user_id": uid, "label": f"Live Acct {uid[-4:]}",
        "broker": "QA Broker", "server": "QABroker-Live", "account_number": "9" + uid[-6:],
        "account_type": "standard", "account_role": "STANDARD",
        "mode": "live", "broker_environment": "LIVE",
        "trading_enabled": True, "status": "connected",
        "bridge_token": f"qa_bt_{uid[-8:]}",
        "last_heartbeat": now.isoformat(), "open_positions": 0,
        "balance": 10000.0, "equity": 10000.0,
        "verified_identity": {"account_number": "9" + uid[-6:],
                              "broker_server": "QABroker-Live",
                              "installation_id": "inst-qa",
                              "verified_at": now.isoformat()},
        "created_at": now})
    db.broker_deals.insert_one({
        "user_id": uid, "account_id": str(acc_id), "deal_id": f"qa-{uid[-6:]}",
        "deal_time": int(time.time()) - 60, "profit": 12.5, "commission": -0.5,
        "swap": 0.0, "deal_entry": "out", "symbol": "XAUUSD"})
    return s


def cleanup_attestation_user(email: str) -> None:
    db = mongo_db()
    u = db.users.find_one({"email": email.lower()}, {"_id": 1})
    if not u:
        return
    uid = str(u["_id"])
    for c in ("accounts", "broker_deals", "performance_shares", "trades",
              "bot_configs", "subscriptions"):
        getattr(db, c).delete_many({"user_id": uid})
    db.users.delete_one({"_id": u["_id"]})
