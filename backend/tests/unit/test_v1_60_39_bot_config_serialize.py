"""v1.60.39 hotfix — GET /api/bot/config must return EVERY BotConfigUpdate field.

15 Oct: _serialize() omitted gold_off_hours_* (and 38 other fields), so BotConfig.jsx fell back to its own default
and the next SAVE wrote that default over the user's value. Any field missing here silently reverts a setting."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytestmark = pytest.mark.unit


def test_serialize_returns_every_bot_config_field_with_model_default_when_unset():
    from models import BotConfigUpdate
    from routes.bot_routes import _serialize
    out = _serialize({"_id": None, "user_id": "u1"})
    missing = sorted(set(BotConfigUpdate.model_fields) - set(out))
    assert missing == [], f"_serialize() omits BotConfig fields (settings would silently revert): {missing}"
    # unset → model default, so the UI never has to invent one
    assert out["gold_off_hours_enabled"] is True
    assert out["gold_off_hours_start_utc"] == "21:00" and out["gold_off_hours_end_utc"] == "06:00"
    assert out["gold_freeze_currencies"] == ["USD", "EUR", "GBP", "JPY", "CNY"]
    assert out["macro_freeze_before_min"] == 15 and out["macro_freeze_avalanche_after_min"] == 20
    assert out["macro_freeze_medium_usd_enabled"] is True and out["macro_freeze_medium_min"] == 5


def test_serialize_round_trips_a_users_stored_values():
    from routes.bot_routes import _serialize
    stored = {"_id": None, "user_id": "u1", "gold_off_hours_enabled": False, "gold_off_hours_start_utc": "22:15",
              "gold_off_hours_end_utc": "00:30", "gold_freeze_currencies": ["USD"], "macro_freeze_medium_min": 3,
              "friday_flat_minutes_before": 90, "news_gate_mode": "advisory"}
    out = _serialize(stored)
    for k, v in stored.items():
        if k not in ("_id",):
            assert out[k] == v, k


def test_new_default_window_is_21_00_to_06_00_everywhere():
    root = os.path.join(os.path.dirname(__file__), "..", "..")
    from agents.execution_optimizer_agent import DEFAULT_OFF_HOURS_END, DEFAULT_OFF_HOURS_START
    assert (DEFAULT_OFF_HOURS_START, DEFAULT_OFF_HOURS_END) == ("21:00", "06:00")
    from models import BotConfigUpdate
    assert BotConfigUpdate().gold_off_hours_end_utc == "06:00"
    fe = open(os.path.join(root, "..", "frontend", "src", "pages", "BotConfig.jsx"), encoding="utf-8").read()
    assert fe.count('gold_off_hours_end_utc || "06:00"') == 2 and 'gold_off_hours_end_utc || "00:30"' not in fe
    assert "default 21:00–06:00" in fe
