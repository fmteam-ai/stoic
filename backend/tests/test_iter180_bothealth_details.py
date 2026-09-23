from live_target import ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
"""iter-180 — /api/bot/health-score details[] shape + unprotected via evaluator.

Covers the review request's specific new assertions:
  * hard_caps[0].code == 'critical_alerts_open' with details[] carrying kind,
    message, occurrences, prior_acked, and UTC-aware ISO timestamps ending +00:00
  * baseline: no unacked critical alerts => score >= 75 and hard_caps == []
  * unprotected_open_query behavior via evaluate_ops_alerts — a broker-confirmed
    trade must NOT trigger the alert; an unconfirmed trade must and its message
    must reference the mt5_ticket of the offending order
"""
import asyncio
import datetime as dt
import os

import pytest
import requests
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

BASE = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]
ADMIN_EMAIL = "admin@stoicaibot.com"
ADMIN_PWD = ADMIN_PASSWORD

UID = "_qa_upq"
TICKET_CONFIRMED = 900101
TICKET_UNCONFIRMED = 900102


def _admin_session():
    s = requests.Session()
    r = s.post(f"{BASE}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PWD}, timeout=30)
    assert r.status_code == 200, f"admin login failed: {r.status_code}"
    csrf = s.cookies.get("csrf_token") or s.cookies.get("csrf")
    if csrf:
        s.headers.update({"X-CSRF-Token": csrf})
    return s


def _cleanup_health(db):
    """Best-effort clear of any unacked critical alerts before baseline check."""
    now = dt.datetime.now(dt.timezone.utc)
    db.ops_alerts.update_many(
        {"acked_at": None, "severity": "critical", "synthetic": {"$ne": True}},
        {"$set": {"acked_at": now, "acked_by": "test:iter180-cleanup"}})


def test_health_score_baseline_no_hard_caps():
    """When no unacked critical alert exists, score>=75 and hard_caps == []."""
    cli = MongoClient(MONGO_URL)
    db = cli[DB_NAME]
    _cleanup_health(db)
    s = _admin_session()
    r = s.get(f"{BASE}/api/bot/health-score", timeout=30)
    assert r.status_code == 200
    d = r.json()
    assert d.get("score", 0) >= 75, f"expected >=75, got {d.get('score')}"
    caps = d.get("hard_caps") or []
    codes = [c.get("code") for c in caps]
    assert "critical_alerts_open" not in codes, f"unexpected cap: {caps}"


def test_health_score_details_shape_with_seeded_alert():
    """Seeded critical alert => hard cap code 'critical_alerts_open' with rich details[]."""
    cli = MongoClient(MONGO_URL)
    db = cli[DB_NAME]
    _cleanup_health(db)
    now = dt.datetime.now(dt.timezone.utc)
    dedup = "_qa_iter180_details"
    seed_id = db.ops_alerts.insert_one({
        "kind": "test_seed",
        "severity": "critical",
        "message": "iter-180 seeded — verify details[] shape",
        "dedup_key": dedup,
        "occurrences": 3,
        "created_at": now,
        "last_seen_at": now,
        "acked_at": None,
        "acked_by": None,
        "synthetic": False,
    }).inserted_id
    # also add a prior acked alert with the same dedup_key so prior_acked > 0
    db.ops_alerts.insert_one({
        "kind": "test_seed",
        "severity": "critical",
        "message": "iter-180 prior acked",
        "dedup_key": dedup,
        "occurrences": 1,
        "created_at": now - dt.timedelta(hours=1),
        "last_seen_at": now - dt.timedelta(hours=1),
        "acked_at": now - dt.timedelta(minutes=30),
        "acked_by": "test",
        "synthetic": False,
    })
    try:
        s = _admin_session()
        r = s.get(f"{BASE}/api/bot/health-score", timeout=30)
        assert r.status_code == 200
        d = r.json()
        caps = d.get("hard_caps") or []
        crit = next((c for c in caps if c.get("code") == "critical_alerts_open"), None)
        assert crit is not None, f"critical_alerts_open cap missing: {caps}"
        details = crit.get("details") or []
        assert details, "details[] missing"
        seeded = next((x for x in details if x.get("message", "").startswith("iter-180 seeded")), None)
        assert seeded is not None, f"seeded row missing from details: {details}"
        for k in ("kind", "message", "occurrences", "prior_acked",
                  "created_at", "last_seen_at"):
            assert k in seeded, f"missing key '{k}' in details entry: {seeded}"
        assert seeded["kind"] == "test_seed"
        assert seeded["occurrences"] == 3
        assert seeded["prior_acked"] >= 1
        # ISO timestamps must be UTC-aware ending in +00:00
        for k in ("created_at", "last_seen_at"):
            assert isinstance(seeded[k], str) and seeded[k].endswith("+00:00"), \
                f"{k} not UTC-aware ISO: {seeded[k]!r}"
        assert d.get("score", 100) <= 45, f"expected cap<=45 got {d.get('score')}"
    finally:
        db.ops_alerts.delete_many({"dedup_key": dedup})


# ---------- unprotected_positions via evaluate_ops_alerts -------------------

def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def _eval_unprotected(mode: str):
    from motor.motor_asyncio import AsyncIOMotorClient
    from alerting import evaluate_ops_alerts

    db = AsyncIOMotorClient(MONGO_URL)[DB_NAME]
    now = dt.datetime.now(dt.timezone.utc)
    opened = (now - dt.timedelta(minutes=10)).isoformat()

    # start clean
    await db.trades.delete_many({"user_id": UID})
    await db.ops_alerts.delete_many({"dedup_key": "unprotected_positions"})
    await db.email_outbox.delete_many({"dedup_key": "unprotected_positions"})

    try:
        if mode == "confirmed_only":
            await db.trades.insert_one({
                "user_id": UID, "status": "open", "symbol": "XAUUSD",
                "opened_at": opened,
                "stop_loss": None, "confirmed_stop_loss": 2400.5,
                "protection_state": "RESOLVED",
                "mt5_ticket": TICKET_CONFIRMED,
            })
        else:  # "with_unconfirmed"
            await db.trades.insert_many([
                {"user_id": UID, "status": "open", "symbol": "XAUUSD",
                 "opened_at": opened,
                 "stop_loss": None, "confirmed_stop_loss": 2400.5,
                 "protection_state": "RESOLVED",
                 "mt5_ticket": TICKET_CONFIRMED},
                {"user_id": UID, "status": "open", "symbol": "EURUSD",
                 "opened_at": opened,
                 "stop_loss": None,
                 "mt5_ticket": TICKET_UNCONFIRMED},
            ])

        await evaluate_ops_alerts(db)
        alert = await db.ops_alerts.find_one(
            {"dedup_key": "unprotected_positions", "acked_at": None})
        return alert
    finally:
        pass  # cleanup done by caller


async def _cleanup():
    from motor.motor_asyncio import AsyncIOMotorClient
    db = AsyncIOMotorClient(MONGO_URL)[DB_NAME]
    await db.trades.delete_many({"user_id": UID})
    await db.ops_alerts.delete_many({"dedup_key": "unprotected_positions"})
    await db.email_outbox.delete_many({"dedup_key": "unprotected_positions"})


def test_unprotected_confirmed_only_no_alert():
    try:
        alert = _run(_eval_unprotected("confirmed_only"))
        assert alert is None, f"unexpected alert for confirmed-only trade: {alert}"
    finally:
        _run(_cleanup())


def test_unprotected_unconfirmed_raises_with_ticket():
    try:
        alert = _run(_eval_unprotected("with_unconfirmed"))
        assert alert is not None, "expected unprotected_positions alert"
        assert alert.get("kind") == "unprotected_positions"
        assert alert.get("severity") == "critical"
        msg = alert.get("message") or ""
        assert str(TICKET_UNCONFIRMED) in msg, \
            f"mt5_ticket {TICKET_UNCONFIRMED} missing in message: {msg!r}"
        # confirmed ticket must NOT appear
        assert str(TICKET_CONFIRMED) not in msg, \
            f"confirmed ticket leaked into alert msg: {msg!r}"
    finally:
        _run(_cleanup())
