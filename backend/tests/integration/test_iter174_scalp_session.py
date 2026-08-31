"""iter-174 — configurable scalp session window + session info in perms."""
import asyncio
import os
import uuid

import pytest

pytestmark = pytest.mark.integration

DB_NAME = f"stoic_test_scalpsess_{uuid.uuid4().hex[:8]}"


async def _scenario():
    os.environ["DB_NAME"] = DB_NAME
    import database
    database._client = None
    from database import get_db
    from scalp.instruments import approved
    from scalp.permissions import _compute
    db = get_db()
    uid = "sess_user"
    cfg = approved("EURUSD")
    try:
        # default window comes from the instrument config
        p = await _compute(db, uid, "EURUSD", cfg)
        s = p["session"]
        assert s["start_utc"] == cfg.session_start_utc
        assert s["end_utc"] == cfg.session_end_utc
        assert s["override"] is False
        if not s["open"]:
            assert s["opens_in_minutes"] > 0
            assert any("outside allowed sessions" in r
                       for r in p["reasons"])

        # 24h override → session always open, reason gone
        await db.scalp_session_windows.insert_one(
            {"_id": f"{uid}:EURUSD", "user_id": uid, "symbol": "EURUSD",
             "start_utc": 0, "end_utc": 24})
        p = await _compute(db, uid, "EURUSD", cfg)
        assert p["session"]["open"] is True
        assert p["session"]["override"] is True
        assert p["session"]["opens_in_minutes"] == 0
        assert not any("outside allowed sessions" in r
                       for r in p["reasons"])

        # invalid stored window falls back to the instrument default
        await db.scalp_session_windows.update_one(
            {"_id": f"{uid}:EURUSD"},
            {"$set": {"start_utc": 22, "end_utc": 3}})
        p = await _compute(db, uid, "EURUSD", cfg)
        assert p["session"]["start_utc"] == cfg.session_start_utc
        assert p["session"]["end_utc"] == cfg.session_end_utc
    finally:
        await db.client.drop_database(DB_NAME)


def test_session_window_override():
    asyncio.run(_scenario())
