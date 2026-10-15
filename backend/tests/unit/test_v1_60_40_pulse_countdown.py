"""v1.60.40 — "NEXT IN" countdown is per account: that account's signal_cooldown_minutes (plan-floored), that
account's own last signal, measured on the server clock and capped at the cooldown (VTMarkets: 33M on a 30-min
cooldown came from the default config + newest signal of any account + client clock skew)."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytestmark = pytest.mark.unit
ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


def test_effective_cooldown_is_per_config_and_plan_floored(monkeypatch):
    from routes.bot_routes import _effective_cooldown_min
    assert _effective_cooldown_min({"signal_cooldown_minutes": 30}) == 30
    assert _effective_cooldown_min({"signal_cooldown_minutes": 2}, plan_floor=15) == 15
    monkeypatch.setenv("BOT_SIGNAL_COOLDOWN_MIN", "7")
    assert _effective_cooldown_min({}) == 7
    assert _effective_cooldown_min({"signal_cooldown_minutes": "garbage"}) == 5


def test_pulse_countdown_is_server_clock_and_capped():
    src = open(os.path.join(ROOT, "routes", "bot_routes.py"), encoding="utf-8").read()
    body = src.split("async def get_bot_pulse(")[1].split("@router.get")[0]
    assert "cooldown_min = _effective_cooldown_min(d, plan_floor)" in body
    assert 'next_in = max(0, min(int((nxt - now).total_seconds()), cooldown_min * 60))' in body
    assert '"next_eligible_in_seconds": next_in' in body and '"cooldown_minutes": cooldown_min' in body
    fe = open(os.path.join(ROOT, "..", "frontend", "src", "components", "BotPulsePanel.jsx"), encoding="utf-8").read()
    assert "item.next_eligible_in_seconds != null" in fe and "fetchedAt" in fe


def test_watch_status_uses_own_account_config_and_own_signal():
    src = open(os.path.join(ROOT, "routes", "signal_routes.py"), encoding="utf-8").read()
    body = src.split("async def watch_status(")[1].split("@router.get")[0]
    assert "account_id: Optional[str] = None" in src.split("async def watch_status(")[1][:60]
    assert '_config_filter(user["id"], account_id)' in body
    assert '"user_id": user["id"]' in body and '"account_id": account_id if account_id else {"$in": [None, ""]}' in body
    assert "_effective_cooldown_min(cfg, int(await cooldown_floor(user) or 1))" in body
    assert "min(cooldown_min * 60 - seconds_since, cooldown_min * 60)" in body
    assert '"own_signal": own_signal' in body
    fe = open(os.path.join(ROOT, "..", "frontend", "src", "components", "BotWatching.jsx"), encoding="utf-8").read()
    assert "watch-account-selector" in fe and "/signals/watch-status${accountId" in fe


def test_countdown_math_never_exceeds_cooldown():
    now = datetime.now(timezone.utc)
    cooldown_min = 30
    # a stale / skewed next_eligible_at 33 min out must be capped at 30 min
    nxt = now + timedelta(minutes=33)
    assert max(0, min(int((nxt - now).total_seconds()), cooldown_min * 60)) == 1800
    # elapsed past the cooldown → 0, never negative
    seconds_since = 31 * 60
    assert max(0, min(cooldown_min * 60 - seconds_since, cooldown_min * 60)) == 0


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)
