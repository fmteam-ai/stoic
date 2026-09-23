from live_target import ADMIN_EMAIL, ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
"""iter125: HTTP E2E — canary promote/rollback for admin user via cookie auth."""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from database import get_db  # noqa: E402

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/") or \
    "https://stoic-trading-bot.preview.emergentagent.com"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def admin_session():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": "admin@stoicaibot.com", "password": ADMIN_PASSWORD},
               timeout=10)
    assert r.status_code == 200, r.text
    # csrf
    s.get(f"{BASE_URL}/api/auth/csrf", timeout=10)
    s.headers["X-CSRF-Token"] = s.cookies.get("csrf_token", "")
    yield s


@pytest.fixture
def seeded_shadow_model(admin_session):
    db = get_db()
    admin = _run(db.users.find_one({"email": "admin@stoicaibot.com"}))
    uid = admin.get("id") or str(admin["_id"])
    from bayes_opt import new_replay_state
    ch = new_replay_state()
    ch.update({"trades": 60, "wins": 40, "losses": 20, "total_r": 30.0,
               "gross_win_r": 55.0, "gross_loss_r": 25.0, "max_dd": 5.0})
    bl = new_replay_state()
    bl.update({"trades": 60, "wins": 25, "losses": 35, "total_r": 5.0,
               "gross_win_r": 30.0, "gross_loss_r": 25.0})
    registered = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    doc = {"user_id": uid, "engine": "trend", "symbol": "XAUUSD",
           "version": f"TEST_iter125~{uuid.uuid4().hex[:10]}",
           "params": {"x": 1.0}, "baseline_params": {"x": 0.5},
           "status": "testing", "registered_at": registered,
           "challenger_state": ch, "baseline_state": bl,
           "_test_marker": "iter125"}
    rid = _run(db.shadow_models.insert_one(doc)).inserted_id
    # ensure bot_configs exists AND is active (canary applies to active configs only)
    _run(db.bot_configs.update_one(
        {"user_id": uid},
        {"$setOnInsert": {"user_id": uid, "engine_params": {},
                          "_test_marker": "iter125"},
         "$set": {"active": True, "_prev_active_iter125": True}},
        upsert=True))
    yield str(rid), uid
    _run(db.shadow_models.delete_one({"_id": rid}))
    _run(db.bot_configs.update_many(
        {"user_id": uid, "_prev_active_iter125": True},
        {"$set": {"active": False},
         "$unset": {"engine_params_canary": "", "_prev_active_iter125": ""}}))


def test_e2e_canary_promote_starts_at_5_and_rollback(admin_session, seeded_shadow_model):
    model_id, uid = seeded_shadow_model
    s = admin_session

    # 1. promote -> should start canary at 5%
    r = s.post(f"{BASE_URL}/api/shadow/models/{model_id}/promote", timeout=10)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("allocation_pct") == 5, body
    assert body.get("ladder") == [5, 10, 25, 50, 100], body

    # 2. bot_configs should have engine_params_canary set
    db = get_db()
    cfg = _run(db.bot_configs.find_one({"user_id": uid, "active": True}))
    assert cfg and "engine_params_canary" in cfg, cfg

    # 3. GET /api/shadow/models/canary shape
    r = s.get(f"{BASE_URL}/api/shadow/models/canary", timeout=10)
    assert r.status_code == 200
    payload = r.json()
    assert payload["ladder"] == [5, 10, 25, 50, 100]
    assert "canaries" in payload and "evaluations" in payload

    # 4. rollback
    r = s.post(f"{BASE_URL}/api/shadow/models/{model_id}/canary/rollback",
               timeout=10)
    assert r.status_code == 200, r.text
    assert r.json().get("status") == "rolled_back"

    cfg = _run(db.bot_configs.find_one({"user_id": uid, "active": True}))
    assert "engine_params_canary" not in cfg or not cfg.get("engine_params_canary", {}).get("trend")


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
