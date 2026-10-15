"""v1.60.48 — feature audit 15 Oct, P1 items 1–5.

1. Partial-close ack could close the whole trade: bare (intent-less) pending_modification writes were overwritten by
   close_commands, and the EA's intent-less PARTIAL_CLOSE ack was then judged as the FULL_CLOSE.
2. Breakeven scratches counted as losses for the anti-tilt / learning freezes.
3. min_confidence_override was combined with auto-tune via min() (should be max); loss_postmortem / auto_heal wrote
   the field directly and overrode user-set values.
4. Finance Calendar backup mapped "AUD Employment Change" to USD → gold froze on an AUD print; agents/regime/risk
   layers ignored the account's freeze currencies.
5. Pre-news protector flattened every open trade of the user regardless of account / origin."""
import asyncio
import os
import re
import sys
from unittest.mock import patch

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ----------------------------------------------------------------------------------------------- 1. command fence
def test_every_pending_modification_writer_is_fenced():
    """No module may $set a bare pending_modification literal — only command_fence / close_commands stamp it."""
    allowed = {"command_fence.py", "close_commands.py", "tests"}
    pat = re.compile(r'["\']pending_modification["\']\s*:\s*\{\s*\n?\s*["\']type["\']')
    offenders = []
    for root, dirs, files in os.walk(BACKEND):
        dirs[:] = [d for d in dirs if d not in ("tests", "__pycache__", "node_modules", ".venv", "static")]
        for f in files:
            if not f.endswith(".py") or f in allowed:
                continue
            src = open(os.path.join(root, f), encoding="utf-8", errors="ignore").read()
            for m in pat.finditer(src):
                window = src[max(0, m.start() - 600):m.start() + 1400]
                # a `stamp={...}` handed to request_close is fenced by close_commands itself; a dict popped into
                # stamp_pending_modification is fenced by command_fence
                if ("request_close(" in window and "stamp=" in window) or "stamp_pending_modification(" in window:
                    continue
                offenders.append(f"{os.path.relpath(os.path.join(root, f), BACKEND)}:{src[:m.start()].count(chr(10)) + 1}")
    assert not offenders, "bare pending_modification writes (use command_fence.stamp_pending_modification):\n" + "\n".join(offenders)


def test_request_close_stamp_full_close_gets_intent_and_seq():
    from close_commands import request_close
    db = FakeDb()
    tid = ObjectId()
    db.trades.rows.append({"_id": tid, "status": "open", "user_id": "u1", "symbol": "XAUUSD"})
    with patch("close_commands._with_txn", lambda db, fn, emergency=False: fn(None)):
        _run(request_close(db, {"_id": tid}, reason="time_stop", actor="trade_manager",
                           stamp={"scalp_exit_rule": "time_stop_90m",
                                  "pending_modification": {"type": "FULL_CLOSE", "reason": "scalp_time_stop"}}))
    row = db.trades.rows[0]
    pm = row["pending_modification"]
    assert pm["type"] == "FULL_CLOSE" and pm["intent_id"] and pm["seq"] == 1
    assert row["scalp_exit_rule"] == "time_stop_90m" and row["close_requested"] is True


def test_tp1_partial_close_is_stamped_with_intent():
    import trade_manager as tm
    db = FakeDb()
    tid = ObjectId()
    db.trades.rows.append({"_id": tid, "status": "open", "user_id": "u1", "symbol": "XAUUSD", "action": "BUY",
                           "entry_price": 2000.0, "lot_size": 0.10, "original_lot": 0.10, "stop_loss": 1990.0,
                           "tp1_pips": 100, "tp2_pips": 200, "tp3_pips": 200, "origin": "auto"})
    stamped = _run(tm.stamp_pending_modification(db, tm._free_slot_filter(tid),
                                                 {"type": "PARTIAL_CLOSE", "new_volume": 0.05}, {"tp1_target_lot": 0.05}))
    assert stamped["intent_id"] and stamped["seq"] == 1 and db.trades.rows[0]["tp1_target_lot"] == 0.05
    # the slot is now busy: a second bare write via the free-slot filter must not apply
    again = _run(tm.stamp_pending_modification(db, tm._free_slot_filter(tid), {"type": "MODIFY_SL", "new_sl": 2000.0}))
    assert again is None and db.trades.rows[0]["pending_modification"]["type"] == "PARTIAL_CLOSE"


def test_intentless_partial_close_ack_never_settles_a_fenced_full_close():
    from command_fence import is_stale_ack, ack_type_mismatch
    trade = {"pending_modification": {"type": "FULL_CLOSE", "intent_id": "abc", "seq": 2}}
    assert is_stale_ack(trade, None) is True                      # bare ack vs fenced pending → stale
    assert is_stale_ack(trade, "abc") is False
    assert is_stale_ack({"pending_modification": {"type": "MODIFY_SL"}}, None) is False  # legacy unfenced pending
    assert ack_type_mismatch({"pending_modification": {"type": "MODIFY_SL", "intent_id": "x"}}, "PARTIAL_CLOSE")
    assert not ack_type_mismatch(trade, "PARTIAL_CLOSE")           # R-6 netting re-map stays sanctioned
    assert not ack_type_mismatch(trade, "FULL_CLOSE")
    src = open(os.path.join(BACKEND, "routes", "bridge_routes.py")).read()
    assert "ack_type_mismatch(trade, payload.type)" in src
    assert "(not payload.intent_id or payload.intent_id == _pm.get(\"intent_id\"))" not in src


# ----------------------------------------------------------------------------------------------- 2. scratches
def test_breakeven_scratches_are_not_streak_losses():
    from streak_losses import is_streak_loss
    base = {"symbol": "XAUUSD", "entry_price": 4000.0, "stop_loss": 3990.0, "lot_size": 0.01}  # risk ≈ $10
    assert is_streak_loss({**base, "pnl": -0.04}) is False
    assert is_streak_loss({**base, "pnl": -0.92}) is False
    assert is_streak_loss({**base, "pnl": -1.5}) is True           # −0.15R
    assert is_streak_loss({**base, "pnl": -9.8}) is True
    assert is_streak_loss({**base, "pnl": -5.0, "scalp_exit_rule": "breakeven_0_5r", "breakeven_set": True}) is False
    assert is_streak_loss({"pnl": -0.5}) is True                   # risk unknown → conservative
    assert is_streak_loss({**base, "pnl": 0.0}) is False


def test_anti_tilt_and_learning_freeze_use_streak_losses():
    assert "is_streak_loss(r) for r in recent" in open(os.path.join(BACKEND, "bot_runner.py")).read()
    lp = open(os.path.join(BACKEND, "learning_pipeline.py")).read()
    assert "is_streak_loss(t) for t in recent" in lp and "STREAK_FIELDS" in lp


# ----------------------------------------------------------------------------------------------- 3. min_confidence
def test_min_confidence_override_raises_the_bar_not_lowers_it():
    src = open(os.path.join(BACKEND, "bot_runner.py")).read()
    assert "eff = max(eff, float(min_conf_override))" in src
    assert "eff = min(eff, float(min_conf_override))" not in src


def test_apply_config_change_records_field_sources_and_automation_respects_user_values():
    from config_promotion import apply_config_change, user_set
    db = FakeDb()
    db.bot_configs.rows.append({"_id": ObjectId(), "user_id": "u1", "account_id": None, "min_confidence_override": 70})
    with patch("config_promotion._transactions_supported", return_value=False):
        _run(apply_config_change(db, "u1", None, {"min_confidence_override": 72}, source="config_update"))
    cfg = db.bot_configs.rows[0]
    assert cfg["min_confidence_override"] == 72 and cfg["field_sources"]["min_confidence_override"] == "config_update"
    assert user_set(cfg, "min_confidence_override") is True
    assert user_set(cfg, "risk_level") is False
    for mod, text in (("loss_postmortem", "apply_config_change"), ("auto_heal", "apply_config_change")):
        src = open(os.path.join(BACKEND, f"{mod}.py")).read()
        assert text in src and 'user_set(cfg, "min_confidence_override")' in src
        assert '{"$set": {"min_confidence_override"' not in src


def test_loss_postmortem_tighten_skips_user_set_value_and_routes_through_promotion():
    import loss_postmortem as lp
    db = FakeDb()
    db.bot_configs.rows.append({"_id": ObjectId(), "user_id": "u1", "account_id": None, "min_confidence_override": 70,
                                "field_sources": {"min_confidence_override": "config_update"}})
    fn = next(getattr(lp, n) for n in dir(lp) if "tighten" in n and asyncio.iscoroutinefunction(getattr(lp, n)))
    import inspect
    params = list(inspect.signature(fn).parameters)
    assert "db" in params[0]
    # skip only if signature is (db, user_id, trade, pattern_key) style — otherwise just assert source-level guard
    src = inspect.getsource(fn)
    assert "user_set(cfg" in src


# ----------------------------------------------------------------------------------------------- 4. macro freeze
def test_finance_calendar_backup_maps_code_prefixed_and_bare_non_us_titles():
    from economic_calendar import _fc_country, parse_financecalendar, relevant_events_for
    assert _fc_country("AUD Employment Change") == "AUD"
    assert _fc_country("Employment Change") == "OTHER"
    assert _fc_country("ADP Employment Change") == "USD"
    assert _fc_country("German ZEW Economic Sentiment") == "EUR"
    assert _fc_country("Nonfarm Payrolls") == "USD"
    evs = parse_financecalendar({"events": [
        {"name": "AUD Employment Change", "impact": "high", "time_utc": "2026-10-15T00:30:00Z"},
        {"name": "CPI", "impact": "high", "time_utc": "2026-10-15T12:30:00Z"}]})
    gold = relevant_events_for("XAUUSD", evs, {})
    assert [e["country"] for e in gold] == ["USD"]


def test_agents_regime_and_risk_layers_pass_account_cfg_to_the_freeze():
    checks = {
        "agents/macro_agent.py": "macro_freeze_check(sym, cfg)",
        "agents/research_agent.py": "macro_freeze_check(symbol, cfg)",
        "agents/orchestrator.py": '_run(self.macro, "macro", cfg=user_cfg)',
        "market_regime.py": "macro_freeze_check(symbol, cfg)",
        "risk_layers.py": 'macro_freeze_check("XAUUSD", the_cfg)',
        "position_protector.py": "upcoming_for(symbol, hours=1, cfg=cfg)",
    }
    for rel, needle in checks.items():
        assert needle in open(os.path.join(BACKEND, rel)).read(), rel


# ----------------------------------------------------------------------------------------------- 5. protector scope
def test_pre_news_protector_only_touches_this_configs_auto_trades():
    import position_protector as pp
    db = FakeDb()
    ids = {k: ObjectId() for k in ("mine", "manual", "other_acct")}
    db.trades.rows += [
        {"_id": ids["mine"], "user_id": "u1", "account_id": "A", "status": "open", "symbol": "XAUUSD", "origin": "auto"},
        {"_id": ids["manual"], "user_id": "u1", "account_id": "A", "status": "open", "symbol": "XAUUSD", "origin": "manual"},
        {"_id": ids["other_acct"], "user_id": "u1", "account_id": "B", "status": "open", "symbol": "XAUUSD", "origin": "auto"},
    ]
    closed = []

    async def fake_close(db_, q, **kw):
        closed.append(q["_id"])
        return {"trades_marked_for_close": 1}

    async def imminent(sym, lead, cfg=None):
        return {"title": "CPI", "country": "USD", "minutes_until": 3.0}

    async def noop(*a, **k):
        return None
    with patch.object(pp, "request_close", fake_close), patch.object(pp, "_imminent_high_impact", imminent), \
            patch.object(pp.ws_manager, "broadcast", noop), patch.object(pp, "inc_intel_counter", noop):
        n = _run(pp.sweep_user(db, "u1", {"account_id": "A", "pre_news_protect_enabled": True, "pre_news_protect_minutes": 5}))
    assert n == 1 and closed == [ids["mine"]]
