from live_target import ADMIN_EMAIL, ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
"""iter-171 — hardening batch: signed single-use order authorizations (#10),
externally-anchorable audit log (#8), slow-query monitoring + ops endpoints
(#9). Host-agent key pinning (#4) and CI random creds (#2) are verified out of
band (PowerShell / GitHub Actions)."""
import os
import sys

import requests

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)
from dotenv import load_dotenv

load_dotenv(os.path.join(_BACKEND_DIR, ".env"))

from live_target import require_live_base_url
BASE_URL = require_live_base_url()
API = f"{BASE_URL}/api"
TIMEOUT = 60


def _run(coro):
    from conftest import run_async
    return run_async(coro)


def _db():
    from database import get_db
    return get_db()


def _admin():
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": "admin@stoicaibot.com", "password": ADMIN_PASSWORD},
               timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return s


# ─── #10 signed single-use atomic order authorization ────────────────
def test_order_authorization_signed_singleuse_atomic():
    import order_authorization as oa
    db = _db()
    uid = f"iter171-{os.urandom(4).hex()}"

    async def scenario():
        try:
            a = await oa.authorize_order(db, user_id=uid, account_id="acc",
                                         symbol="EURUSD", side="BUY")
            # replay: same nonce can't be consumed twice
            replay = await oa.consume(db, nonce=a["nonce"], user_id=uid,
                                      account_id="acc", symbol="EURUSD",
                                      side="BUY")
            # tamper: a fresh mint consumed with a different binding fails
            m = await oa.mint(db, user_id=uid, account_id="acc",
                              symbol="XAUUSD", side="SELL")
            tamper = await oa.consume(db, nonce=m["nonce"], user_id=uid,
                                      account_id="acc", symbol="XAUUSD",
                                      side="BUY")
            doc = await db.order_authorizations.find_one({"nonce": a["nonce"]})
            return a, replay, tamper, doc
        finally:
            await db.order_authorizations.delete_many({"user_id": uid})
    a, replay, tamper, doc = _run(scenario())
    assert a["ok"] and a["nonce"] and a["signature"]
    assert replay["ok"] is False and replay["reason"] == "not_found_or_replayed"
    assert tamper["ok"] is False  # binding mismatch
    assert doc["status"] == "consumed" and doc["consumed_at"]
    assert len(doc["signature"]) == 64  # hmac-sha256 hex


def test_order_authorization_signature_verifies():
    """The stored signature must equal an independent HMAC over the binding."""
    import order_authorization as oa
    db = _db()
    uid = f"iter171sig-{os.urandom(4).hex()}"

    async def scenario():
        try:
            m = await oa.mint(db, user_id=uid, account_id="a1",
                              symbol="GBPUSD", side="BUY")
            expected = oa._sign(oa._binding(uid, "a1", "GBPUSD", "BUY",
                                            m["nonce"]))
            return m["signature"], expected
        finally:
            await db.order_authorizations.delete_many({"user_id": uid})
    got, expected = _run(scenario())
    assert got == expected


# ─── #8 externally-anchorable audit log ──────────────────────────────
def test_audit_anchor_create_and_verify():
    import audit_anchor as aa
    db = _db()

    async def scenario():
        anchor = await aa.create_anchor(db)
        v = await aa.verify_latest(db)
        # idempotent per head
        again = await aa.create_anchor(db)
        return anchor, v, again
    anchor, v, again = _run(scenario())
    if anchor is None:
        # no audit-chain entries in this DB — verify handles gracefully
        assert v["ok"] and v["anchored"] is False
        return
    assert anchor["signature"] and anchor["seq"] >= 0
    assert v["ok"] and v["signature_valid"] and v["chain_covers_anchor"]
    assert v["anchored_entry_intact"]
    assert again["seq"] == anchor["seq"]  # not re-anchored


def test_audit_anchor_endpoints():
    s = _admin()
    r = s.get(f"{API}/ops/audit-anchor", timeout=TIMEOUT)
    assert r.status_code == 200 and "ok" in r.json()
    r = requests.get(f"{API}/ops/audit-anchor", timeout=TIMEOUT)
    assert r.status_code == 403


# ─── #9 slow-query monitoring ────────────────────────────────────────
def test_query_perf_endpoint():
    s = _admin()
    r = s.get(f"{API}/ops/query-perf", timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) >= {"total", "slow", "slowest_ms", "threshold_ms"}
    assert body["total"] >= 1  # this very request was observed
    r = requests.get(f"{API}/ops/query-perf", timeout=TIMEOUT)
    assert r.status_code == 403


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
