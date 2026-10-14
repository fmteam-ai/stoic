"""v1.60.32 items 2+3 — Telegram settings live on a dedicated doc; separate verifiable alert e-mail."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_legacy_binding_on_a_notice_row_is_migrated_once_and_survives_the_notice_deletion():
    import notification_settings as ns
    db = FakeDb()
    uid = str(ObjectId())
    notice = {"_id": ObjectId(), "user_id": uid, "kind": "security_new_login", "title": "New login", "read": False,
              "telegram_bot_token": {"ct": "sealed"}, "telegram_chat_id": "12345", "telegram_enabled": True,
              "telegram_verified": True, "alerts": {"trade_opened": True}}
    db.notifications.rows.append(notice)
    s = _run(ns.get_settings(db, uid))
    assert s["kind"] == ns.KIND and s["telegram_chat_id"] == "12345" and s["telegram_verified"] is True
    assert s["migrated_from"] == str(notice["_id"])
    # the notice keeps its own fields but no longer carries the binding
    assert notice["title"] == "New login" and "telegram_bot_token" not in notice and "alerts" not in notice
    # second read is a plain hit, no second settings doc
    _run(ns.get_settings(db, uid))
    assert sum(1 for r in db.notifications.rows if r.get("kind") == ns.KIND) == 1
    # deleting the notice no longer deletes the configuration
    db.notifications.rows.remove(notice)
    assert _run(ns.get_settings(db, uid))["telegram_chat_id"] == "12345"


def test_update_settings_always_pins_the_dedicated_doc():
    import notification_settings as ns
    db = FakeDb()
    uid = "u1"
    db.notifications.rows.append({"_id": ObjectId(), "user_id": uid, "kind": "security_new_login", "title": "x"})
    _run(ns.update_settings(db, uid, {"$set": {"telegram_chat_id": "777", "telegram_enabled": True}}))
    docs = [r for r in db.notifications.rows if r.get("kind") == ns.KIND]
    assert len(docs) == 1 and docs[0]["telegram_chat_id"] == "777"
    assert "telegram_chat_id" not in db.notifications.rows[0]          # the notice is untouched
    assert _run(ns.by_webhook_secret(db, "nope")) is None


def test_no_raw_user_id_access_to_db_notifications_remains():
    import re
    bad = []
    for base in (BACKEND, os.path.join(BACKEND, "routes")):
        for name in os.listdir(base):
            if name.endswith(".py") and name != "notification_settings.py":
                src = open(os.path.join(base, name), encoding="utf-8").read()
                if re.search(r'db\.notifications\.(find_one|update_one)\(\s*\{\s*"user_id"', src):
                    bad.append(name)
    assert not bad, bad


def test_notifier_reads_the_settings_doc():
    import notifier
    db = FakeDb()
    uid = "u9"
    db.notifications.rows.append({"_id": ObjectId(), "user_id": uid, "kind": "telegram_settings", "telegram_enabled": True,
                                  "telegram_bot_token": {"ct": "x"}, "telegram_chat_id": "42", "alerts": {"trail": False}})
    with patch.object(notifier, "get_db", lambda: db), patch.object(notifier, "vault_decrypt", lambda blob: "tok"):
        cfg = _run(notifier._get_user_telegram(uid))
    assert cfg == {"token": "tok", "chat_id": "42", "alerts": {"trail": False}}


def test_alert_email_is_separate_verifiable_and_falls_back_to_login_email():
    import notification_settings as ns
    db = FakeDb()
    uid = str(ObjectId())
    db.users.rows.append({"_id": ObjectId(uid), "email": "login@example.com"})
    assert _run(ns.alert_email_for(db, uid)) == "login@example.com"
    _run(ns.update_settings(db, uid, {"$set": {"alert_email": "ops@example.com", "alert_email_verified": False}}))
    assert _run(ns.alert_email_for(db, uid)) == "login@example.com"            # unverified → still the login e-mail
    addr, code = _run(ns.start_email_verification(db, uid))
    assert addr == "ops@example.com" and len(code) == 6
    assert _run(ns.confirm_email_verification(db, uid, "000000")) in ("wrong_code",)
    assert _run(ns.confirm_email_verification(db, uid, code)) == ""
    assert _run(ns.alert_email_for(db, uid)) == "ops@example.com"
    # expired challenge
    _run(ns.update_settings(db, uid, {"$set": {"alert_email_verified": False, "alert_email_verify": {
        "hash": "x", "attempts": 0, "expires_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()}}}))
    assert _run(ns.confirm_email_verification(db, uid, "123456")) == "challenge_expired"
    assert ns.valid_email("a@b.co") and not ns.valid_email("nope") and not ns.valid_email("a@b")


def test_routes_and_ui_expose_the_alert_email():
    src = open(os.path.join(BACKEND, "routes", "notification_routes.py"), encoding="utf-8").read()
    assert '"/email/verify/start"' in src and '"/email/verify/confirm"' in src
    assert "dest = await alert_email_for(db, user[\"id\"], user.get(\"email\"))" in src
    assert '"alert_email": doc.get("alert_email")' in src
    root = os.path.dirname(BACKEND)
    page = open(os.path.join(root, "frontend", "src", "pages", "Notifications.jsx"), encoding="utf-8").read()
    panel = open(os.path.join(root, "frontend", "src", "components", "AlertTestPanel.jsx"), encoding="utf-8").read()
    assert "alert-email-input" in page and "alert_email: alertEmail.trim()" in page
    assert "email-verify-start" in panel and "/notifications/email/verify/confirm" in panel
    assert "alert_email_for" in open(os.path.join(BACKEND, "heartbeat_watch.py"), encoding="utf-8").read()
    assert "alert_email_for" in open(os.path.join(BACKEND, "pairing_alerts.py"), encoding="utf-8").read()
