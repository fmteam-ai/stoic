"""v1.60.48 — feature audit 15 Oct, P2/P3 items 6, 7, 10, 11, 12, 14, 15, 16."""
import asyncio
import os
import sys
from unittest.mock import patch

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FRONTEND = os.path.join(os.path.dirname(BACKEND), "frontend", "src")


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ----------------------------------------------------------------------------------------------- 6. TP rescaling
def test_vol_retarget_expansion_is_capped_at_1_2():
    from adaptive_exits import vol_retarget, VOL_RETARGET_MAX_SCALE
    assert VOL_RETARGET_MAX_SCALE == 1.2
    trade = {"symbol": "XAUUSD", "sl_pips": 100, "atr15_at_entry": 1.0, "tp_pips": [100, 200, 200]}
    act = vol_retarget(trade, {"atr15": 1.6}, 10.0)
    assert act["scale"] == 1.2 and act["new_tp_pips"] == [120.0, 240.0, 240.0]


def test_trend_ride_widens_only_after_the_range_gate_and_only_if_the_widened_tp_fits():
    src = open(os.path.join(BACKEND, "bot_runner.py")).read()
    i_gate = src.index('rg = range_gate(signal["action"], signal.get("entry_price"),')
    i_ride = src.index("widen_signal_targets(signal, _mult, _base)")
    assert i_gate < i_ride, "trend-ride must be applied AFTER the range gate judged the base target"
    assert "_probe.get(\"tp1\") or _probe.get(\"take_profit\"), rf) is None" in src
    assert "trend_ride_skipped_range" in src


# ----------------------------------------------------------------------------------------------- 7. per-user cap
def test_user_direction_cap_default_and_wiring():
    from bot_runner import USER_DIRECTION_CAP_DEFAULT
    from models import BotConfigUpdate
    assert USER_DIRECTION_CAP_DEFAULT == 1 and BotConfigUpdate().user_direction_cap == 1
    src = open(os.path.join(BACKEND, "bot_runner.py")).read()
    assert 'user_q = {k: v for k, v in pyramid_q.items() if k != "account_id"}' in src
    assert "user_direction_cap_block" in src
    from strategy_presets import STRICTER_MIN
    assert "user_direction_cap" in STRICTER_MIN


# ----------------------------------------------------------------------------------------------- 10. PATCH save
def test_bot_config_page_sends_only_changed_fields_and_audit_lists_changed_fields():
    page = open(os.path.join(FRONTEND, "pages", "BotConfig.jsx")).read()
    assert "changedFields(fullPayload, baselineRef.current)" in page
    assert "baselineRef.current = withFlatDefaults(data)" in page
    lib = open(os.path.join(FRONTEND, "lib", "configDrift.js")).read()
    assert "export function changedFields" in lib
    routes = open(os.path.join(BACKEND, "routes", "bot_routes.py")).read()
    assert 'changed_fields = sorted(k for k, v in update.items() if (current_cfg or {}).get(k) != v)' in routes
    assert '"fields": sorted(update.keys())' not in routes


# ----------------------------------------------------------------------------------------------- 11. loss advisor
def test_loss_advisor_friday_flat_is_scoped_to_loss_accounts_and_respects_user_fields():
    import loss_advisor as la
    db = FakeDb()
    db.bot_configs.rows += [
        {"_id": ObjectId(), "user_id": "u1", "account_id": "A", "friday_flat_enabled": False},
        {"_id": ObjectId(), "user_id": "u1", "account_id": "B", "friday_flat_enabled": False,
         "field_sources": {"friday_flat_enabled": "config_update"}},
        {"_id": ObjectId(), "user_id": "u1", "account_id": "C", "friday_flat_enabled": False},
    ]
    db.users.rows.append({"_id": ObjectId("0" * 24), "auto_apply_measures": True})
    measure = {"type": "friday_flat", "params": {"mode": "close"}, "title": "Friday flat",
               "evidence": {"testable": True, "net_effect": 500.0, "losses_avoided": 600.0, "wins_missed": 50.0,
                            "distinct_signals": 50}}
    review = {"measures": [measure], "loss_account_ids": ["A", "B"]}

    async def enabled(db_, uid):
        return True

    async def suspended(db_, uid):
        return None
    with patch.object(la, "_auto_apply_enabled", enabled), patch.object(la, "_auto_apply_suspended", suspended), \
            patch("config_promotion._transactions_supported", return_value=False):
        applied = _run(la._auto_apply(db, "u1", review))
    by_acct = {c["account_id"]: c for c in db.bot_configs.rows}
    assert by_acct["A"]["friday_flat_enabled"] is True and by_acct["A"]["friday_flat_mode"] == "close"
    assert by_acct["B"]["friday_flat_enabled"] is False, "user-set field must not be overridden"
    assert by_acct["B"].get("friday_flat_mode") == "close", "non-user-set field on the same config still applies"
    assert by_acct["C"]["friday_flat_enabled"] is False, "account without losses untouched"
    assert applied and applied[0]["updated_configs"] == 2 and applied[0]["accounts"] == ["A", "B"]
    assert "update_many" not in open(os.path.join(BACKEND, "loss_advisor.py")).read().split("mtype == \"friday_flat\"")[1].split("elif mtype")[0]


# ----------------------------------------------------------------------------------------------- 12. preset overlay
def test_preset_overlay_takes_the_stricter_value_for_caps_and_cooldowns():
    from strategy_presets import safe_overlay
    user = {"risk_level": "low", "max_concurrent_trades": 1, "trade_of_day_cap": 1, "min_confidence_override": 80,
            "signal_cooldown_minutes": 30, "sl_cooldown_minutes": 60, "anti_tilt_enabled": True,
            "pre_news_protect_enabled": True, "trailing_start_r": 1.5}
    preset = {"risk_level": "high", "max_concurrent_trades": 4, "trade_of_day_cap": 6, "min_confidence_override": 60,
              "signal_cooldown_minutes": 5, "sl_cooldown_minutes": 15, "anti_tilt_enabled": False,
              "pre_news_protect_enabled": False, "trailing_start_r": 1.0, "partial_close_fraction": 0.3}
    out = safe_overlay(user, preset)
    assert out["risk_level"] == "low" and out["max_concurrent_trades"] == 1 and out["trade_of_day_cap"] == 1
    assert out["min_confidence_override"] == 80 and out["signal_cooldown_minutes"] == 30 and out["sl_cooldown_minutes"] == 60
    assert out["anti_tilt_enabled"] is True and out["pre_news_protect_enabled"] is True
    assert out["trailing_start_r"] == 1.0 and out["partial_close_fraction"] == 0.3   # behaviour knobs still overlay
    # a preset may still TIGHTEN
    assert safe_overlay({"max_concurrent_trades": 3, "min_confidence_override": 0}, {"max_concurrent_trades": 1, "min_confidence_override": 70}) \
        == {"max_concurrent_trades": 1, "min_confidence_override": 70}
    for rel in ("bot_runner.py", "meta_strategy.py", "ai_optimizer.py"):
        assert "safe_overlay(" in open(os.path.join(BACKEND, rel)).read(), rel


# ----------------------------------------------------------------------------------------------- 14 / 15. model
def test_escalate_fields_accepted_and_defaults_have_one_source():
    from models import BotConfigUpdate
    u = BotConfigUpdate(daily_profit_target_escalate=True, daily_profit_target_escalate_step_r=2.0)
    d = u.model_dump(exclude_unset=True)
    assert d == {"daily_profit_target_escalate": True, "daily_profit_target_escalate_step_r": 2.0}
    import bot_runner
    defaults = BotConfigUpdate().model_dump()
    assert bot_runner.CFG_DEFAULTS["risk_level"] == defaults["risk_level"] == "low"
    assert bot_runner.CFG_DEFAULTS["max_concurrent_trades"] == 1 and bot_runner.CFG_DEFAULTS["spread_filter_enabled"] is True
    routes = open(os.path.join(BACKEND, "routes", "bot_routes.py")).read()
    assert "**BotConfigUpdate().model_dump()," in routes and '"risk_level": "medium"' not in routes
    src = open(os.path.join(BACKEND, "bot_runner.py")).read()
    for bad in ('cfg.get("risk_level", "medium")', 'cfg.get("max_concurrent_trades", 3)', 'cfg.get("spread_filter_enabled", False)'):
        assert bad not in src, bad


def test_new_config_doc_uses_model_defaults():
    import routes.bot_routes as br
    db = FakeDb()
    db.accounts.rows.append({"_id": ObjectId("1" * 24), "user_id": "u1"})

    async def scope(db_, uid, aid):
        return aid

    async def mode(db_, aid):
        return "shadow"
    with patch.object(br, "_resolve_account_scope", scope), patch.object(br, "default_operational_mode", mode):
        cfg = _run(br._get_or_create_config(db, "u1", "1" * 24))
    assert cfg["risk_level"] == "low" and cfg["max_concurrent_trades"] == 1 and cfg["spread_filter_enabled"] is True
    assert cfg["active"] is False and cfg["user_direction_cap"] == 1 and cfg["operational_mode"] == "shadow"


# ----------------------------------------------------------------------------------------------- 16. paused cfg
def test_trade_on_paused_per_account_config_is_managed_by_its_own_config():
    import trade_manager as tm
    db = FakeDb()
    db.bot_configs.rows += [
        {"_id": ObjectId(), "user_id": "u1", "account_id": None, "active": True, "tag": "default"},
        {"_id": ObjectId(), "user_id": "u1", "account_id": "B", "active": False, "tag": "paused-B"},
    ]
    db.trades.rows += [
        {"_id": ObjectId(), "user_id": "u1", "account_id": "B", "status": "open", "symbol": "XAUUSD", "mt5_ticket": 1},
        {"_id": ObjectId(), "user_id": "u1", "account_id": "Z", "status": "open", "symbol": "XAUUSD", "mt5_ticket": 2},
    ]
    seen = []

    async def manage(t, cfg):
        seen.append((t["account_id"], cfg["tag"]))

    assert not hasattr(tm, "_check_daily_drawdown"), "audit item 13 — circuit_breakers is the single daily-drawdown breaker"
    assert not hasattr(tm, "_soft_stop_should_fire"), "audit item 8 — dead soft-stop knob removed"
    with patch.object(tm, "get_db", lambda: db), patch.object(tm, "_manage_one_trade", manage):
        _run(tm._tick())
    assert ("B", "paused-B") in seen, seen
    assert ("Z", "default") in seen, seen


# ----------------------------------------------------------------------------------------------- 8 / 17. dead fields
def test_dead_config_fields_are_gone_from_model_presets_and_page():
    from models import BotConfigUpdate, DEAD_CONFIG_FIELDS
    from strategy_presets import PRESETS
    fields = set(BotConfigUpdate.model_fields)
    assert not (fields & DEAD_CONFIG_FIELDS)
    assert BotConfigUpdate().gold_off_hours_end_utc == "07:00"
    for p in PRESETS.values():
        assert not (set(p.get("config") or {}) & DEAD_CONFIG_FIELDS)
    page = open(os.path.join(FRONTEND, "pages", "BotConfig.jsx")).read()
    for f in ("breakeven_enabled", "trailing_enabled", "soft_stop_enabled", "asia_session_skip_xau", "partial_close_enabled"):
        assert f"field=\"{f}\"" not in page, f
    assert 'data-testid="engine-exit-rules"' in page and "90 min" in page
    src = open(os.path.join(BACKEND, "bot_runner.py")).read()
    assert "asia_session_skip_xau" not in src.replace("# v1.60.48 (audit item 17) — the Asia-session skip merged", "")


def test_legacy_asia_skip_configs_keep_their_coverage_until_07_00():
    from agents.execution_optimizer_agent import off_hours_window
    assert off_hours_window({}) == (21 * 60, 7 * 60)
    assert off_hours_window({"gold_off_hours_end_utc": "06:00", "asia_session_skip_xau": True}) == (21 * 60, 7 * 60)
    assert off_hours_window({"gold_off_hours_end_utc": "06:00"}) == (21 * 60, 6 * 60)
    assert off_hours_window({"gold_off_hours_end_utc": "00:30", "asia_session_skip_xau": False}) == (21 * 60, 30)
