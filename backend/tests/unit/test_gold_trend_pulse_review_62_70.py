"""gold_trend_pulse review items #62–#70 (before shadow enable)."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
pytestmark = pytest.mark.unit
BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gold_trend_pulse import engine as E  # noqa: E402
from gold_trend_pulse import rules as R  # noqa: E402


def _run(c):
    return asyncio.new_event_loop().run_until_complete(c)


def test_62_trend_ride_and_tp_rescale_excluded_for_deterministic_scopes():
    br = open(os.path.join(BACKEND, "bot_runner.py")).read()
    i = br.index("if ride and _det_scope:")
    assert "ride = None" in br[i:i + 400] and br.index("_det_scope = signal.get(\"scope\") in _DET") < i
    from strategy_engines import DETERMINISTIC_INTRADAY_SCOPES
    assert "gold_trend_pulse" in DETERMINISTIC_INTRADAY_SCOPES and "hf_scalp" in DETERMINISTIC_INTRADAY_SCOPES
    tm = open(os.path.join(BACKEND, "trade_manager.py")).read()
    assert tm.index('== "gold_trend_pulse":') < tm.index("TP ladder rescales to live ATR")   # GTP returns before vol-retarget


def test_63_spread_lookup_by_broker_symbol_fails_closed_when_missing_or_stale():
    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(seconds=10)).isoformat()
    for key in ("GOLD#", "XAUUSD+", "XAUUSD.crp", "XAUUSD"):
        sp, why = E._spread_price({"current_spreads": {key: 2.5}, "spreads_updated_at": fresh}, None, now)
        assert sp == pytest.approx(0.25) and why is None, key
    sp, why = E._spread_price({"current_spreads": {"EURUSD": 1.0}, "spreads_updated_at": fresh}, None, now)
    assert sp is None and "spread unknown" in why and "entry refused" in why
    sp, why = E._spread_price({"current_spreads": {"GOLD#": 2.5}, "spreads_updated_at": (now - timedelta(seconds=500)).isoformat()}, None, now)
    assert sp is None and "stale" in why
    sp, why = E._spread_price({"current_spreads": {"GOLD#": 2.5}}, None, now)
    assert sp is None and "no timestamp" in why
    eng = open(os.path.join(BACKEND, "gold_trend_pulse", "engine.py")).read()
    assert "if spread is None:\n        return await _done(sp_why)" in eng
    assert '(account.get("current_spreads") or {}).get(SYMBOL)' not in eng


def test_64_demo_live_only_attested_demo_enum_validated_confirm_live_everywhere_rolling_gate_basket_once(monkeypatch):
    from pydantic import ValidationError
    from models import BotConfigUpdate
    with pytest.raises(ValidationError):
        BotConfigUpdate(gold_trend_pulse_mode="live")
    assert BotConfigUpdate(gold_trend_pulse_mode="demo_live").gold_trend_pulse_mode == "demo_live"
    br = open(os.path.join(BACKEND, "routes", "bot_routes.py")).read()
    assert 'update.get("gold_trend_pulse_mode") == "demo_live"' in br and "gtp_confirm_live_required" in br
    rt = open(os.path.join(BACKEND, "routes", "gold_trend_pulse_routes.py")).read()
    assert "demo_attested(acc)" in rt and "confirm_live" in rt
    assert not E.demo_attested({"account_type": "demo", "mode": "live", "trade_mode": "real"})      # broker says real → never DEMO

    async def _gate_ok(db, account_id, now=None):
        return {"eligible": True, "reasons": [], "stats": {}}
    monkeypatch.setattr(E, "live_gate", _gate_ok)
    mode, gate = _run(E.effective_mode(None, {"gold_trend_pulse_mode": "demo_live"}, "a", {"account_type": "standard", "mode": "live"}))
    assert mode == "shadow" and "not attested DEMO" in gate["reasons"][0]
    monkeypatch.setattr(E, "demo_attested", lambda acc: True)
    assert _run(E.effective_mode(None, {"gold_trend_pulse_mode": "demo_live"}, "a", {}))[0] == "demo_live"
    # rolling window + basket counted once
    eng = open(os.path.join(BACKEND, "gold_trend_pulse", "engine.py")).read()
    assert "GATE_WINDOW_DAYS" in eng and 'recent_outcomes(db, account_id, "demo_live", since)' in eng
    rows = E.collapse_baskets([{"r": 1.0, "pnl_usd": 10, "basket_id": "b1", "closed_at": "2026-10-16T10:00"},
                               {"r": -1.0, "pnl_usd": -8, "basket_id": "b1", "closed_at": "2026-10-16T10:30"},
                               {"r": 0.5, "pnl_usd": 4, "basket_id": None, "closed_at": "2026-10-16T11:00"}])
    assert len(rows) == 2 and rows[0]["legs"] == 2 and rows[0]["r"] == 0.0 and rows[0]["pnl_usd"] == 2 and rows[0]["closed_at"].endswith("10:30")


def test_65_layered_legs_need_stop_side_tp_cap_and_l1_confirmation():
    eng = open(os.path.join(BACKEND, "gold_trend_pulse", "engine.py")).read()
    i, j = eng.index("conf = R.detect_entry(data[\"m5\"], tracker.bias, vwap)"), eng.index('plan = {"ok": True, "tp": round(tp, 2), "sl": sl_dist')
    block = eng[i:j]
    assert "R.detect_entry(data[\"m5\"], tracker.bias, vwap)" in block and "shared stop not beyond entry" in block and "R.SL_MAX_TP_MULT * tp" in block


def test_66_management_runs_during_macro_freeze_and_protection_waits_for_the_ack():
    ai = open(os.path.join(BACKEND, "ai_signals.py")).read()
    i = ai.index('if macro.get("frozen"):')
    assert "management_only=True" in ai[i:i + 900] and ai[i:i + 900].index("management_only=True") < ai[i:i + 900].index('return _hold(f"Macro freeze')
    eng = open(os.path.join(BACKEND, "gold_trend_pulse", "engine.py")).read()
    assert "if management_only:" in eng and eng.index("if management_only:") > eng.index("_news_protect(db, live, shadow") and \
        eng.index("if management_only:") < eng.index("ok, why = R.session_ok(now)")
    i = eng.index("async def _news_protect")
    body = eng[i:eng.index("\ndef _decision")]
    assert "be_clamped_skip(db, t, new_sl, price" in body and '"gtp_news_protect_pending": key' in body
    assert body.index('t.get("gtp_news_protect_pending") == key') < body.index("entry = float(t[\"entry_price\"])")
    assert 'if not t.get("pending_modification"):' in body        # protected only after the EA cleared the modification


def test_67_complete_m5_bars_completed_h1_h4_warmup_and_broker_unknown_basis():
    t0 = 1_760_000_000 // 300 * 300
    m1 = [{"t": t0 + i * 60, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1} for i in range(9)]    # 5 + 4 (incomplete second bar)
    m5 = E.complete_m5(m1, t0 + 9 * 60 + 1)
    assert len(m5) == 1 and m5[0]["t"] == t0 and m5[0]["n"] == 5
    m5 = E.complete_m5(m1 + [{"t": t0 + 9 * 60, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}], t0 + 600)
    assert len(m5) == 2
    assert E.complete_m5(m1[:5] + m1[6:], t0 + 600) == [m5[0]]           # a missing M1 inside the second bar → not evaluated
    eng = open(os.path.join(BACKEND, "gold_trend_pulse", "engine.py")).read()
    assert 'R.completed(R.resample(m15, 3600), 3600, ts)' in eng and 'load_user_candles(SYMBOL, user_id, "H4"' in eng and E.H4_MIN_BARS == 200
    assert 'if out["t_basis"] == "broker_unknown":' in eng and "H4 warm-up" in eng


def test_68_sweep_scalp_leg_obeys_1_3_tp_cap():
    assert not R.plan_sweep_exit("BUY", 4149.8, 4145.0, 4158.0, 1.7)["ok"]
    p = R.plan_sweep_exit("BUY", 4146.0, 4145.0, 4158.0, 1.7)
    assert p["ok"] and p["sl"] <= 1.3 * p["tp"] and p["runner"]["take_profit"] == 4158.0


def test_69_scratch_not_loss_and_r_from_original_stop():
    rows = [{"r": 1.0}, {"r": -0.02, "result": "scratch"}, {"r": -0.01, "net_price": -0.1, "costs_price": 0.32}, {"r": -1.0}]
    st = R.outcome_stats(rows)
    assert st["losses"] == 1 and st["scratch"] == 2 and st["wins"] == 1 and st["profit_factor"] == 1.0
    assert R.consecutive_losses([{"r": -1.0}, {"r": -0.01, "result": "scratch"}]) == 1 and R.consecutive_losses([{"r": -1.0}, {"r": 0.5}, {"r": -1.0}]) == 1
    assert R.daily_stop(R.consecutive_losses([{"r": -1}, {"r": -1}, {"r": 0, "result": "scratch"}]), 0, 0) is None
    from gold_trend_pulse.engine import label_with_bars
    now = datetime(2026, 10, 16, 14, 0, tzinfo=timezone.utc)
    d = {"direction": "BUY", "entry": 4150.0, "take_profit": 4153.0, "stop_loss": 4148.0, "tp_dist": 3.0, "sl_dist": 2.0, "spread_at_entry": 0.25,
         "commission_price": 0.07, "created_at": (now - timedelta(minutes=10)).isoformat(), "lots": 0.01}
    t = int((now - timedelta(minutes=9)).timestamp()) // 60 * 60
    bars = [{"t": t, "o": 4150, "h": 4152.0, "l": 4149.9, "c": 4151.9}, {"t": t + 60, "o": 4151.9, "h": 4152, "l": 4149.0, "c": 4150.3}]
    lab = label_with_bars(d, bars, now)
    assert lab["exit_reason"] == "breakeven" and lab["result"] == "scratch" and not R.is_loss(lab)
    eng = open(os.path.join(BACKEND, "gold_trend_pulse", "engine.py")).read()
    assert 'stop = t.get("original_stop_loss") or t.get("stop_loss")' in eng
    from trade_manager import scalp_cost_pips
    assert scalp_cost_pips({"symbol": "XAUUSD", "gold_trend_pulse": {"spread_at_entry": 0.18}}) == pytest.approx(1.8)   # stored spread, not the 2.5 default


def test_70_exact_event_match_high_impact_opened_cap_dead_code_and_user_offset():
    assert R.news_event_kind("Economic Optimism") is None and R.news_event_kind("ISM Manufacturing PMI") == "ISM"
    assert R.news_event_kind("Prelim GDP q/q") == "GDP" and R.news_event_kind("Average Hourly Earnings") is None
    t = 1_760_000_000.0
    low = [{"title": "CPI m/m", "country": "USD", "impact": "medium", "when_ts": t}]
    assert R.news_phase(low, datetime.fromtimestamp(t + 600, tz=timezone.utc)) is None                 # medium impact ignored
    assert R.next_news_event(low, datetime.fromtimestamp(t - 600, tz=timezone.utc)) is None
    eng = open(os.path.join(BACKEND, "gold_trend_pulse", "engine.py")).read()
    assert "opened_today = await db.gtp_decisions.count_documents" in eng and 'RANGE: breakout — no new range trades' not in eng
    rp = open(os.path.join(BACKEND, "scripts", "gtp_replay.py")).read()
    assert "if a.csv else 0" in rp
