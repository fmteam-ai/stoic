"""iter-87 · Migration Helper — export/import the admin's state.

Covers:
  · GET /api/admin/export-state — admin-only, returns accounts + bot_configs
    + user_presets owned by the admin, with serialised ObjectIds.
  · POST /api/admin/import-state — admin-only, idempotent upsert; remaps
    user_id to the target admin; preserves bridge_token + encrypted creds.
  · Schema-version mismatch → 400.
  · Non-admin → 403 on both endpoints.
"""
from __future__ import annotations
import os
import uuid

import pytest
import requests
from bson import ObjectId
from pymongo import MongoClient

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    with open("/app/frontend/.env") as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL"):
                BASE_URL = line.split("=", 1)[1].strip().strip('"').rstrip("/")
if not BASE_URL.startswith("http"):
    BASE_URL = "https://" + BASE_URL
TIMEOUT = 30


def _mongo():
    with open("/app/backend/.env") as f:
        cfg = {ln.split("=", 1)[0]: ln.split("=", 1)[1].strip().strip("\"'")
               for ln in f if "=" in ln}
    return MongoClient(cfg["MONGO_URL"])[cfg["DB_NAME"]]


def _admin_session() -> requests.Session:
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": "admin@trading.bot", "password": "admin123"},
               timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return s


def _register_verified() -> tuple[str, requests.Session]:
    suffix = uuid.uuid4().hex[:10]
    email, pw = f"iter87_{suffix}@example.com", "password123"
    r = requests.post(f"{BASE_URL}/api/auth/register",
                      json={"email": email, "password": pw,
                            "name": f"iter87-{suffix}", "terms_agreed": True},
                      timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    uid = r.json()["id"]
    _mongo().users.update_one(
        {"_id": ObjectId(uid)},
        {"$set": {"email_verified": True},
         "$unset": {"activation_token": "", "activation_expires_at": ""}},
    )
    s = requests.Session()
    s.post(f"{BASE_URL}/api/auth/login",
           json={"email": email, "password": pw}, timeout=TIMEOUT)
    return uid, s


# ─── Guards ─────────────────────────────────────────────────────────────
def test_export_state_requires_admin():
    _, sess = _register_verified()
    r = sess.get(f"{BASE_URL}/api/admin/export-state", timeout=TIMEOUT)
    assert r.status_code == 403


def test_import_state_requires_admin():
    _, sess = _register_verified()
    r = sess.post(f"{BASE_URL}/api/admin/import-state",
                  json={"schema_version": 1, "user": {}, "collections": {}},
                  timeout=TIMEOUT)
    assert r.status_code == 403


def test_export_state_returns_admin_state():
    sess = _admin_session()
    r = sess.get(f"{BASE_URL}/api/admin/export-state", timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["schema_version"] == 1
    assert body["exported_by"] == "admin@trading.bot"
    assert body["user"]["email"] == "admin@trading.bot"
    # Should NOT leak password_hash.
    assert "password_hash" not in body["user"]
    assert set(body["collections"].keys()) >= {"accounts", "bot_configs", "user_presets"}
    # Counts are consistent with the document lists.
    for coll, docs in body["collections"].items():
        assert body["counts"][coll] == len(docs)


def test_import_state_schema_mismatch_rejected():
    sess = _admin_session()
    r = sess.post(f"{BASE_URL}/api/admin/import-state",
                  json={"schema_version": 999, "user": {}, "collections": {}},
                  timeout=TIMEOUT)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "schema_mismatch"


def test_import_state_idempotent_roundtrip():
    """Export → import on the same env should be a no-op (modify_count == 0
    on the second pass; first pass may update timestamps but not duplicate)."""
    sess = _admin_session()
    exported = sess.get(f"{BASE_URL}/api/admin/export-state", timeout=TIMEOUT).json()
    # First import
    r1 = sess.post(f"{BASE_URL}/api/admin/import-state",
                   json=exported, timeout=TIMEOUT)
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    assert body1["ok"] is True
    # Second import — same payload, should NOT insert anything new (all
    # existing _ids matched).
    r2 = sess.post(f"{BASE_URL}/api/admin/import-state",
                   json=exported, timeout=TIMEOUT)
    assert r2.status_code == 200
    body2 = r2.json()
    for coll in ("accounts", "bot_configs", "user_presets"):
        assert body2["collections"][coll]["inserted"] == 0, \
            f"{coll}: re-import created duplicates"


def test_import_state_new_account_lands_in_admin_collection():
    """Import a synthetic account row → it should be queryable by the admin."""
    sess = _admin_session()
    # Build a minimal valid export payload with one synthetic account
    fake_oid = str(ObjectId())
    synthetic = {
        "_id": fake_oid,
        "user_id": "WILL_BE_REMAPPED",
        "label": f"iter87-synthetic-{uuid.uuid4().hex[:6]}",
        "broker": "TEST_BROKER",
        "server": "T-Demo",
        "account_number": f"iter87-{uuid.uuid4().hex[:8]}",
        "account_type": "demo",
        "base_currency": "USD",
        "mode": "live",
        "bridge_token": "test-token-from-source-env",
        "status": "disconnected",
    }
    payload = {
        "schema_version": 1,
        "user": {"email": "admin@trading.bot"},
        "collections": {"accounts": [synthetic], "bot_configs": [], "user_presets": []},
    }
    r = sess.post(f"{BASE_URL}/api/admin/import-state", json=payload, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["collections"]["accounts"]["inserted"] == 1

    # Verify the doc landed with user_id remapped to the admin's id
    db = _mongo()
    admin = db.users.find_one({"email": "admin@trading.bot"})
    doc = db.accounts.find_one({"_id": ObjectId(fake_oid)})
    assert doc is not None
    assert doc["user_id"] == str(admin["_id"])
    # bridge_token survived the round-trip (so EAs on the user's VPS keep working)
    assert doc["bridge_token"] == "test-token-from-source-env"

    # Cleanup
    db.accounts.delete_one({"_id": ObjectId(fake_oid)})
