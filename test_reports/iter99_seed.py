"""Seed a synthetic account + pairing for iter-99 UI test.
Prints ACCOUNT_ID + EA_DEPLOYMENT_ID for later cleanup."""
import os
import uuid
import json
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

import requests
from pymongo import MongoClient
from bson import ObjectId

BASE = open("/app/frontend/.env").read().split(
    "REACT_APP_BACKEND_URL=")[1].split("\n")[0].strip().strip('"')
API = f"{BASE}/api"

s = requests.Session()
r = s.post(f"{API}/auth/login",
           json={"email": "admin@trading.bot", "password": "admin123"},
           timeout=15)
assert r.status_code == 200, r.text
csrf = s.cookies.get("csrf_token")
if csrf:
    s.headers.update({"X-CSRF-Token": csrf})
me = s.get(f"{API}/auth/me").json()
uid = me["id"]

client = MongoClient(os.environ["MONGO_URL"])
db = client[os.environ["DB_NAME"]]

label = f"TEST_iter99_ui_{uuid.uuid4().hex[:6]}"
res = db.accounts.insert_one({
    "user_id": uid, "mode": "demo", "label": label,
    "server": "TEST-Server",
    "broker_account_id_reported": "9990000",
    "bridge_token": f"tok_seed_{uuid.uuid4().hex}"})
aid = str(res.inserted_id)

r = s.post(f"{API}/infra/pairing", json={"account_id": aid}, timeout=15)
assert r.status_code == 200, r.text
p = r.json()

print(json.dumps({"account_id": aid, "ea_deployment_id": p["ea_deployment_id"],
                  "code": p["code"], "label": label}))
