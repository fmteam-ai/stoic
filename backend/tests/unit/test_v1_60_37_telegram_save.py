"""v1.60.37 — PUT /notifications/telegram only resets the handshake when the token or chat_id actually CHANGES.
The form always sends chat_id, so enabling alerts after verifying used to un-verify the binding."""
import asyncio
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _settings_doc(db):
    import notification_settings as ns
    return next(r for r in db.notifications.rows if r.get("kind") == ns.KIND)


def _save(db, user, **fields):
    import routes.notification_routes as nr
    payload = nr.TelegramConfigIn(**fields)
    with patch.object(nr, "get_db", lambda: db):
        return _run(nr.update_telegram(payload, user=user))


@pytest.fixture
def verified(monkeypatch):
    import notification_settings as ns
    monkeypatch.setattr("secrets_vault.encrypt", lambda s: f"enc:{s}")
    monkeypatch.setattr("secrets_vault.decrypt", lambda s: s[4:])
    import routes.notification_routes as nr
    monkeypatch.setattr(nr, "vault_encrypt", lambda s: f"enc:{s}")
    monkeypatch.setattr(nr, "vault_decrypt", lambda s: s[4:])
    db = FakeDb()
    user = {"id": "u1", "email": "u@x.io", "role": "user"}
    _run(ns.update_settings(db, "u1", {"$set": {"telegram_bot_token": "enc:123:ABC", "telegram_chat_id": "777",
                                                "telegram_verified": True, "telegram_enabled": False}}))
    return db, user


def test_enabling_alerts_with_the_same_chat_id_keeps_the_binding_verified(verified):
    db, user = verified
    out = _save(db, user, telegram_enabled=True, telegram_chat_id="777")           # what the form sends
    assert out["telegram_verified"] is True and out["telegram_enabled"] is True
    out = _save(db, user, telegram_enabled=True, telegram_chat_id=" 777 ", telegram_bot_token="123:ABC")
    assert out["telegram_verified"] is True                                          # same values, whitespace only


def test_changing_token_or_chat_id_still_requires_a_fresh_handshake(verified):
    db, user = verified
    assert _save(db, user, telegram_enabled=True, telegram_chat_id="778")["telegram_verified"] is False
    doc = _settings_doc(db)
    doc["telegram_verified"] = True
    assert _save(db, user, telegram_enabled=True, telegram_bot_token="999:NEW", telegram_chat_id="778")["telegram_verified"] is False
    assert _settings_doc(db)["telegram_bot_token"] == "enc:999:NEW"
    doc = _settings_doc(db)
    doc["telegram_verified"] = True
    assert _save(db, user, telegram_enabled=False, telegram_bot_token="")["telegram_verified"] is False   # clearing = change
    assert _settings_doc(db)["telegram_bot_token"] is None
