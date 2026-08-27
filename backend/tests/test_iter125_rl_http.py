"""iter125: HTTP-level rate-limit smoke test using a seeded API key.

Bypasses the /api/api-keys creation endpoint (which requires MFA step-up)
by seeding directly in db.api_keys, then hammers GET /api/v1/accounts
via the public URL.
"""
import asyncio
import hashlib
import os
import secrets
import sys
from datetime import datetime, timezone

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from database import get_db  # noqa: E402

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/") or \
    "https://stoic-trading-bot.preview.emergentagent.com"
PREFIX_LEN = len("stoic_live_") + 8


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _generate_key():
    raw = secrets.token_urlsafe(32)
    full = f"stoic_live_{raw}"
    key_hash = hashlib.sha256(full.encode()).hexdigest()
    return full, full[:PREFIX_LEN], key_hash


@pytest.fixture
def seeded_admin_key():
    db = get_db()
    admin = _run(db.users.find_one({"email": "admin@stoicaibot.com"}))
    assert admin, "admin@stoicaibot.com missing"
    full, prefix, key_hash = _generate_key()
    doc = {
        "user_id": admin.get("id") or str(admin["_id"]),
        "name": "TEST_iter125_rl",
        "key_prefix": prefix,
        "key_hash": key_hash,
        "scopes": ["read:accounts"],
        "rate_limit_per_minute": 5,
        "revoked_at": None,
        "last_used_at": None,
        "total_requests": 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    res = _run(db.api_keys.insert_one(doc))
    # also clear rate buckets for this key
    _run(db.rate_buckets.delete_many({"key_id": str(res.inserted_id)}))
    yield full
    _run(db.api_keys.delete_one({"_id": res.inserted_id}))
    _run(db.rate_buckets.delete_many({"key_id": str(res.inserted_id)}))


def test_rate_limit_5_per_minute_read(seeded_admin_key):
    codes = []
    last_body = None
    for i in range(7):
        r = requests.get(f"{BASE_URL}/api/v1/accounts",
                         headers={"X-API-Key": seeded_admin_key},
                         timeout=10)
        codes.append(r.status_code)
        last_body = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
    print("codes:", codes, "last:", last_body)
    # First 5 should succeed (200), 6th onward -> 429
    ok_count = sum(1 for c in codes[:5] if c == 200)
    assert ok_count == 5, f"expected first 5 to be 200, got {codes[:5]}"
    assert codes[5] == 429, f"6th should be 429, got {codes[5]}"
    assert isinstance(last_body, dict), last_body
    detail = last_body.get("detail", {})
    assert detail.get("error") == "rate_limited", detail


def test_rate_limiter_uses_mongo_backend(seeded_admin_key):
    """REDIS_URL not set in preview -> Mongo backend."""
    # trigger one request
    requests.get(f"{BASE_URL}/api/v1/accounts",
                 headers={"X-API-Key": seeded_admin_key}, timeout=10)
    db = get_db()
    docs = _run(db.rate_buckets.find({}).to_list(length=10))
    assert len(docs) >= 1, "expected rate_buckets doc after request"
