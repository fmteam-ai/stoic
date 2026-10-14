"""v1.60.32 — adopted-trade alerts · alert e-mail per category · 422 without body echo."""
import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(BACKEND)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _src(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


# ── adopted trade alerts ───────────────────────────────────────────────────────────────────────────────────────
def test_adopted_open_sends_the_bot_trade_alert_exactly_once():
    from routes import bridge_routes as br
    db = FakeDb()
    tid = ObjectId()
    db.trades.rows.append({"_id": tid, "user_id": "u1", "status": "open", "symbol": "EURUSD", "action": "BUY",
                           "mt5_ticket": 123456789, "entry_price": 1.1, "lot_size": 0.1, "origin": "AI signal"})
    sent = AsyncMock(return_value=True)
    with patch("notifier.notify_trade_opened", sent):
        assert _run(br.notify_adopted_open(db, "u1", tid, "heartbeat")) is True
        assert _run(br.notify_adopted_open(db, "u1", tid, "external_deal")) is False      # idempotent
    assert sent.await_count == 1 and sent.await_args.args[0] == "u1" and sent.await_args.args[1]["_id"] == tid
    row = db.trades.rows[0]
    assert row["notified_opened"] is True and row["notified_via"] == "heartbeat"
    # a row that is not open (pending / cancelled) never alerts
    pid = ObjectId()
    db.trades.rows.append({"_id": pid, "user_id": "u1", "status": "pending"})
    with patch("notifier.notify_trade_opened", sent):
        assert _run(br.notify_adopted_open(db, "u1", pid, "heartbeat")) is False
    assert sent.await_count == 1


def test_every_adoption_and_ingest_path_calls_the_alert():
    src = _src("backend", "routes", "bridge_routes.py")
    assert 'await notify_adopted_open(db, acc["user_id"], sib["_id"], "heartbeat")' in src
    assert 'await notify_adopted_open(db, acc["user_id"], ins.inserted_id, "heartbeat_backfill")' in src
    assert 'await notify_adopted_open(db, user_id, sibling["_id"], "external_deal")' in src
    assert 'await notify_adopted_open(db, user_id, result.inserted_id, "external_deal_ingest")' in src
    # /report keeps honouring the same flag → no double alert whichever path lands first
    assert 'if payload.status == "open" and not trade.get("notified_opened")' in src


# ── alert e-mail per category ──────────────────────────────────────────────────────────────────────────────────
def test_alert_email_categories_route_each_mail_class():
    import notification_settings as ns
    db = FakeDb()
    uid = str(ObjectId())
    db.users.rows.append({"_id": ObjectId(uid), "email": "login@example.com"})
    _run(ns.update_settings(db, uid, {"$set": {"alert_email": "ops@example.com", "alert_email_verified": True,
                                               "alert_email_categories": {"trading": True, "renewal": False, "support": False}}}))
    assert _run(ns.alert_email_for(db, uid, "login@example.com", category="trading")) == "ops@example.com"
    assert _run(ns.alert_email_for(db, uid, "login@example.com", category="outage")) == "ops@example.com"   # unset → on
    assert _run(ns.alert_email_for(db, uid, "login@example.com", category="renewal")) == "login@example.com"
    assert _run(ns.alert_email_for(db, uid, "login@example.com", category="support")) == "login@example.com"
    assert set(ns.ALERT_EMAIL_CATEGORIES) == {"trading", "outage", "renewal", "support", "digest"}


def test_every_user_facing_mail_class_is_routed_and_security_mail_is_not():
    assert 'category="renewal"' in _src("backend", "background_loops.py")
    assert 'category="support"' in _src("backend", "routes", "support_routes.py")
    assert 'category="digest"' in _src("backend", "routes", "insights_routes.py")
    assert 'category="outage"' in _src("backend", "heartbeat_watch.py")
    assert 'category="outage"' in _src("backend", "pairing_alerts.py")
    assert 'category="trading"' in _src("backend", "routes", "notification_routes.py")
    for f in ("login_alerts.py", "login_otp.py", "password_reset.py", "degraded_login_otp.py"):
        assert "alert_email_for" not in _src("backend", f), f
    page = _src("frontend", "src", "pages", "Notifications.jsx")
    assert "alert-email-categories" in page and "alert_email_categories: emailCats" in page
    routes = _src("backend", "routes", "notification_routes.py")
    assert "alert_email_categories: Optional[Dict[str, bool]]" in routes and '"alert_email_categories":' in routes


# ── 422 hygiene ────────────────────────────────────────────────────────────────────────────────────────────────
def test_validation_errors_do_not_echo_the_request_body():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from pydantic import BaseModel
    import server

    app = FastAPI()
    app.add_exception_handler(__import__("fastapi.exceptions", fromlist=["RequestValidationError"]).RequestValidationError,
                              server._validation_error_without_echo)

    class Body(BaseModel):
        email: str
        password: str
        code: str

    @app.post("/x")
    async def x(b: Body):
        return {"ok": True}

    r = TestClient(app).post("/x", json={"email": "a@b.co", "password": "hunter2-S3cret"})
    assert r.status_code == 422
    body = r.text
    assert "hunter2-S3cret" not in body and '"input"' not in body
    d = r.json()["detail"]
    assert d and d[0]["loc"] == ["body", "code"] and d[0]["msg"] and d[0]["type"]
    assert "@app.exception_handler(RequestValidationError)" in _src("backend", "server.py")
