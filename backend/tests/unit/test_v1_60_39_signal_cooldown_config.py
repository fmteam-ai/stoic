"""v1.60.39 — signal_cooldown_minutes is a first-class Bot Config field ("Minutes between signals", per account),
floored by the plan (features.min_signal_cooldown_minutes). It was only reachable via API/presets before, so
STARTRADER / VTMarkets ran with 30 min and nobody could see or change it."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytestmark = pytest.mark.unit
ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


def test_model_field_and_serialize_default(monkeypatch):
    from models import BotConfigUpdate
    from routes.bot_routes import _serialize
    f = BotConfigUpdate.model_fields["signal_cooldown_minutes"]
    assert f.default == 5
    with pytest.raises(Exception):
        BotConfigUpdate(signal_cooldown_minutes=0)
    assert BotConfigUpdate(signal_cooldown_minutes=30).signal_cooldown_minutes == 30
    # unset → the runtime default the bot_runner really uses (env, then 5)
    monkeypatch.setenv("BOT_SIGNAL_COOLDOWN_MIN", "7")
    assert _serialize({"user_id": "u"})["signal_cooldown_minutes"] == 7
    monkeypatch.delenv("BOT_SIGNAL_COOLDOWN_MIN")
    assert _serialize({"user_id": "u"})["signal_cooldown_minutes"] == 5
    assert _serialize({"user_id": "u", "signal_cooldown_minutes": 30})["signal_cooldown_minutes"] == 30


def test_runner_clamps_to_plan_floor_like_the_route():
    from bot_runner import _cooldown_minutes
    assert _cooldown_minutes({"signal_cooldown_minutes": 2, "_tier_min_cooldown": 15}) == 15
    assert _cooldown_minutes({"signal_cooldown_minutes": 30, "_tier_min_cooldown": 15}) == 30
    assert _cooldown_minutes({"signal_cooldown_minutes": 0}) == 1


def test_put_config_clamps_to_plan_floor_and_reports_it():
    src = open(os.path.join(ROOT, "routes", "bot_routes.py"), encoding="utf-8").read()
    body = src.split("async def update_config(")[1]
    assert 'if "signal_cooldown_minutes" in update:' in body
    assert "cooldown_floor(user)" in body and 'update["signal_cooldown_minutes"] = max(asked, floor)' in body
    assert '"cooldown_clamped_to_plan_floor"' in body and '"requested": asked, "floor": floor' in body
    get_body = src.split("async def get_config(")[1].split("@router.get")[0]
    assert "_attach_cooldown_floor(out, user)" in get_body
    helper = src.split("async def _attach_cooldown_floor(")[1]
    assert '"plan_min_signal_cooldown_minutes"' in helper and '"effective_signal_cooldown_minutes"' in helper


def test_bot_config_ui_exposes_the_field():
    fe = open(os.path.join(ROOT, "..", "frontend", "src", "pages", "BotConfig.jsx"), encoding="utf-8").read()
    assert "signal_cooldown_minutes: cfg.signal_cooldown_minutes," in fe          # in the save payload
    for tid in ("signal-cooldown-input", "signal-cooldown-floor", "signal-cooldown-floor-warning"):
        assert tid in fe, tid
    assert "MINUTES BETWEEN SIGNALS" in fe and "cooldown_clamped_to_plan_floor" in fe
