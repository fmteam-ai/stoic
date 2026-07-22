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
    url = os.environ.get("REACT_APP_BACKEND_URL")
    if not url:
        try:
            with open(os.path.join(os.path.dirname(BACKEND),
                                   "frontend", ".env")) as f:
                for line in f:
                    if line.startswith("REACT_APP_BACKEND_URL="):
                        url = line.split("=", 1)[1].strip().strip('"')
        except OSError:
            pass
    if not url:
        raise RuntimeError("REACT_APP_BACKEND_URL not set")
    return url.rstrip("/")


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
        {"$set": {"current_plan_id": "elite_monthly", "valid_until": valid}},
        upsert=True)


def register_and_login(email: str, password: str = "pass12345",
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
