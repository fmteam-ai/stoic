"""v1.60.36 (revised) — scalp exits + evidence:
1 trend-ride stays ON for scalps; every trade records trend_ride_applied
2 tp1/tp2/tp3 stay in sync with the broker TP (trend-ride) and the adaptive "TP ladder rescaled" output
3 SL/TP re-based to the actual fill when |fill − signal| > 2 pips (SL now; TP once the EA honours new_tp)
4 slippage measured against the signal price
5 Strategy Report per engine (MC bands · entry position in day range · with/without trend-ride), deduped by signal
6 failure_classifier: a routine broker-confirmed backfill is NOT an operational failure
7 Loss Lab / Auto Loss Review / shadow tests: one decision per signal across mirrored accounts
8 auto-apply evidence bar: ≥ 20 distinct signals
9 DXY / VIX / 10Y / MTF / setup score recorded at entry for scalp engines too
"""
import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock, patch

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── 1 ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_trend_ride_stays_on_for_scalps_and_is_recorded_per_trade():
    src = _read("backend", "bot_runner.py")
    i = src.index("trend_ride_check(signal[\"action\"], _feats, _base)")
    assert "_ride_exempt" not in src[i - 600:i]                          # no scalp skip
    assert '"trend_ride_applied": bool(signal.get("trend_ride"))' in src
    ex = _read("backend", "execution.py")
    assert '"trend_ride_applied": bool(signal.get("trend_ride"))' in ex and '"entry_context": signal.get("entry_context")' in ex


# ── 2 ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_trend_ride_widens_take_profit_and_tp_fields_together():
    from exit_sync import widen_signal_targets
    sig = {"entry_price": 4080.0, "action": "SELL", "take_profit": 4066.1, "tp1": 4066.1, "tp2": 4066.1, "tp3": 4066.1,
           "tp_pips": [139.0, 139.0, 139.0]}
    assert widen_signal_targets(sig, 1.8, "XAUUSD") is True
    assert sig["take_profit"] == 4060.0 and sig["tp3"] == sig["take_profit"] and sig["tp2"] == 4060.0 and sig["tp1"] == 4070.0
    assert sig["tp_pips"] == [100.0, 200.0, 200.0]
    sig = {"entry_price": 1.1000, "action": "BUY", "take_profit": 1.1030, "tp1": 1.1010, "tp2": 1.1020, "tp3": 1.1030,
           "tp_pips": [10, 20, 30]}
    widen_signal_targets(sig, 1.5, "EURUSD")
    assert sig["take_profit"] == 1.1045 and sig["tp1"] == 1.1015 and sig["tp2"] == 1.103 and sig["tp3"] == 1.1045


def test_vol_retarget_rescales_tp_fields_with_the_pip_ladder():
    import adaptive_exits as ae
    from exit_sync import tp_fields_from_pips
    assert tp_fields_from_pips(4000.0, "SELL", "XAUUSD", [100, 200, 200]) == {"tp1": 3990.0, "tp2": 3980.0, "tp3": 3980.0}
    db = FakeDb()
    db.trades.rows.append({"_id": ObjectId(), "user_id": "u1", "symbol": "XAUUSD", "action": "BUY", "entry_price": 4000.0,
                           "stop_loss": 3990.0, "sl_pips": 100.0, "tp_pips": [100.0, 200.0, 200.0], "tp1": 4010.0,
                           "tp2": 4020.0, "tp3": 4020.0, "status": "open", "atr15_at_entry": 6.0})
    db.intraday_candles.rows.append({"user_id": "u1", "symbol": "XAUUSD", "timeframe": "M15", "bars": [{"t": time.time()}]})
    act = {"kind": "EXIT_RETARGET_VOL", "new_tp_pips": [160.0, 320.0, 320.0], "atr_ratio": 1.6, "scale": 1.6}
    with patch.object(ae, "vol_retarget", lambda *a, **k: act), patch.object(ae, "compute_features", lambda bars: {"atr15": 9.6}), \
            patch("ws_manager.manager") as ws, patch("trade_events.append", AsyncMock()):
        ws.broadcast = AsyncMock()
        assert _run(ae.manage_exits(db, db.trades.rows[0], {}, current=4005.0, pips_up=50.0)) is True
    row = db.trades.rows[0]
    assert row["tp_pips"] == [160.0, 320.0, 320.0] and row["tp1"] == 4016.0 and row["tp2"] == 4032.0 and row["tp3"] == 4032.0


# ── 3 ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def _sell_gold():
    return {"_id": ObjectId(), "symbol": "XAUUSD", "action": "SELL", "entry_price": 4080.0, "stop_loss": 4086.95,
            "take_profit": 4066.1, "tp1": 4066.1, "tp2": 4066.1, "tp3": 4066.1, "sl_pips": 69.5, "status": "open"}


def test_fill_rebase_moves_sl_and_tp_to_the_fill_keeping_pip_distances():
    from exit_sync import fill_rebase
    rb = fill_rebase(_sell_gold(), 4079.45)                              # 5.5 pips worse for a SELL
    assert rb["gap_pips"] == 5.5 and rb["new_sl"] == 4086.4 and round((rb["new_sl"] - 4079.45) / 0.1, 1) == 69.5
    assert rb["new_tp"] == 4065.55 and rb["tp_fields"] == {"take_profit": 4065.55, "tp1": 4065.55, "tp2": 4065.55, "tp3": 4065.55}
    assert round((4079.45 - rb["new_tp"]) / 0.1, 1) == 139.0            # TP distance preserved from the fill
    buy = {**_sell_gold(), "action": "BUY", "stop_loss": 4073.05, "take_profit": 4093.9}
    rb = fill_rebase(buy, 4080.6)
    assert rb["new_sl"] == 4073.65 and rb["new_tp"] == 4094.5


def test_fill_rebase_is_skipped_for_small_gaps_and_never_loosens():
    from exit_sync import fill_rebase
    assert fill_rebase(_sell_gold(), 4079.85) is None                    # 1.5 pips
    assert fill_rebase(_sell_gold(), 4080.6) is None                     # favourable → would loosen
    assert fill_rebase({**_sell_gold(), "sl_pips": 0}, 4079.0) is None


def test_bridge_wires_rebase_command_and_applies_tp_only_on_broker_confirmation():
    br = _read("backend", "routes", "bridge_routes.py")
    i = br.index("rb = fill_rebase(trade, actual)")
    assert '"type": "MODIFY_SL", "new_sl": rb["new_sl"], "new_tp": rb.get("new_tp"), "reason": "fill_rebase"' in br[i:i + 900]
    assert "confirmed_position_tp: float | None = None" in br
    j = br.index('rbf = (trade.get("fill_rebase") or {}).get("tp_fields")')
    assert "payload.confirmed_position_tp and rbf.get(\"take_profit\")" in br[j:j + 400] and "update.update(rbf)" in br[j:j + 500]


# ── 4 ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_slippage_is_measured_against_the_signal_price_too():
    from exit_sync import signal_slippage
    assert signal_slippage("SELL", 4080.0, 4079.45, "XAUUSD") == (5.5, 5.5)
    assert signal_slippage("BUY", 4080.0, 4080.6, "XAUUSD") == (6.0, 6.0)
    assert signal_slippage("SELL", 4080.0, 4080.6, "XAUUSD") == (0.0, -6.0)
    br = _read("backend", "routes", "bridge_routes.py")
    assert 'update["signal_slippage_pips"], update["signal_gap_pips"] = signal_slippage(action, intended, actual, symbol)' in br


# ── 5 ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def _closed(sig, pnl, acct, opened="2026-10-14T13:46:43+00:00", action="SELL", **kw):
    # v1.60.37 — each account has its OWN signal doc; mirrored rows share symbol + direction + entry (±2 min)
    t = {"_id": ObjectId(), "user_id": "u1", "status": "closed", "origin": "auto", "symbol": "XAUUSD", "action": action,
         "pnl": pnl, "signal_id": str(ObjectId()) if sig == "own" else sig, "account_id": acct, "scope": "hf_scalp_fast",
         "engine": None, "opened_at": opened, "closed_at": "2026-10-14T18:00:00+00:00"}
    t.update(kw)
    return t


def test_strategy_report_bands_dedupe_mirrored_accounts_and_split_trend_ride():
    from strategy_report import build_report, decisions_from
    s1, s2, s3 = str(ObjectId()), str(ObjectId()), str(ObjectId())
    signals = {s1: {"monte_carlo": {"p_tp_first": 0.35}, "session_feats": {"range_pos_pct": 6.0}, "trend_ride": None},
               s2: {"monte_carlo": {"p_tp_first": 0.55}, "session_feats": {"range_pos_pct": 55.0}, "trend_ride": {"direction": "down"}},
               s3: {"monte_carlo": {"p_tp_first": 0.33}, "session_feats": {"range_pos_pct": 8.0}, "trend_ride": None}}
    # OnEquity 13:46:43 / RoboForex 13:46:44 / third account 13:47:30 → ONE decision although 3 signal docs
    trades = [_closed(s1, -40, "a"), _closed("own", -44, "b", opened="2026-10-14T13:46:44+00:00"),
              _closed("own", -36, "c", opened="2026-10-14T13:47:30+00:00"),
              _closed(s2, 120, "a", opened="2026-10-14T15:10:00+00:00", action="BUY", trend_ride_applied=True),
              _closed("own", 110, "b", opened="2026-10-14T15:10:05+00:00", action="BUY", trend_ride_applied=True),
              _closed(s3, -30, "a", opened="2026-10-14T13:50:00+00:00")]          # 3 min 17 s later → separate decision
    dec = decisions_from(trades, signals)
    assert len(dec) == 3 and dec[0]["pnl"] == -40.0 and dec[0]["mirrored_accounts"] == 3
    buy = next(d for d in dec if d["key"].startswith("XAUUSD|BUY"))
    assert buy["trend_ride"] is True and buy["mirrored_accounts"] == 2 and buy["pnl"] == 115.0
    rep = build_report(dec, 30)
    assert rep["decisions"] == 3 and rep["trades"] == 6
    eng = rep["engines"][0]
    assert eng["engine"] == "hf_scalp_fast" and eng["decisions"] == 3 and eng["win_rate"] == 33.3
    low = next(r for r in eng["range_position"] if r["band"] == "0–10%")
    assert low["n"] == 2 and low["win_rate"] == 0.0                                        # "sold too close to the low"
    high = next(r for r in eng["range_position"] if r["band"] == "40%+")
    assert high["n"] == 1 and high["win_rate"] == 100.0
    mc = next(r for r in eng["mc_bands"] if r["band"] == "30–40%")
    assert mc["n"] == 2 and mc["predicted_win_rate"] == 34.0 and mc["win_rate"] == 0.0
    ride = {r["band"]: r for r in eng["trend_ride"]}
    assert ride["with trend-ride"]["n"] == 1 and ride["with trend-ride"]["win_rate"] == 100.0 and ride["without"]["n"] == 2
    assert '@router.get("/strategy-report")' in _read("backend", "routes", "postmortem_routes.py")
    assert "<StrategyReport />" in _read("frontend", "src", "pages", "LossLab.jsx")


# ── 6 ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_routine_broker_confirmed_backfill_is_not_an_operational_failure():
    from failure_classifier import classify_failure
    routine = {"pnl": -40, "close_reason": "broker_confirmed", "backfilled_at": "2026-10-14T18:00:00+00:00",
               "backfill_gap": False, "slippage_pips": 0.0, "symbol": "XAUUSD"}
    v = classify_failure(routine, {}, [], {})
    assert v["category"] != "operational_failure", v
    gap = {**routine, "backfill_gap": True}
    assert classify_failure(gap, {}, [], {})["category"] == "operational_failure"
    # legacy rows (no backfill_gap key): only ghost / reconcile / adopted markers count
    legacy_routine = {k: v for k, v in routine.items() if k != "backfill_gap"}
    assert classify_failure(legacy_routine, {}, [], {})["category"] != "operational_failure"
    assert classify_failure({**legacy_routine, "auto_repaired_from_ghost": True}, {}, [], {})["category"] == "operational_failure"
    br = _read("backend", "routes", "bridge_routes.py")
    assert 'update["backfill_gap"] = bool(existing.get("pnl_unknown") or existing.get("pnl_estimated")' in br


# ── 7 / 8 ──────────────────────────────────────────────────────────────────────────────────────────────────────
def test_loss_review_counts_one_decision_per_signal_and_requires_20_signals_to_auto_apply():
    import loss_advisor as la
    s1 = str(ObjectId())
    ds = {"trades": [_closed(s1, -40, "a"), _closed("own", -44, "b", opened="2026-10-14T13:46:44+00:00"),
                     _closed("own", -36, "c", opened="2026-10-14T13:47:00+00:00"),
                     _closed(None, 20, "a", opened="2026-10-14T16:00:00+00:00")],
          "signals": {s1: {"confidence": 50, "regime": "CHOP", "session": "london"}}, "losses": [], "since_loss": "2026-10-01"}
    agg = la._aggregates(ds)
    assert agg["trades"] == 4 and agg["decisions"] == 2 and agg["loss_decisions"] == 1
    ev = la._shadow_test({"type": "min_confidence", "params": {"value": 60}}, ds)
    assert ev["trades_blocked"] == 1 and ev["distinct_signals"] == 1 and ev["losses_avoided"] == 40.0   # average, not ×3
    assert la.AUTO_APPLY_MIN_SIGNALS == 20
    strong = {"evidence": {"testable": True, "net_effect": 900.0, "losses_avoided": 900.0, "wins_missed": 0.0, "distinct_signals": 19}}
    assert la._qualifies(strong) is False
    strong["evidence"]["distinct_signals"] = 20
    assert la._qualifies(strong) is True
    pm = _read("backend", "routes", "postmortem_routes.py")
    assert '"count": len(group_decisions(rows))' in pm and '"$addToSet"' not in pm
    assert '"opened_at": trade.get("opened_at")' in _read("backend", "loss_postmortem.py")


def test_decision_grouping_is_symbol_direction_and_entry_within_two_minutes():
    from decision_groups import group_decisions
    rows = [_closed("own", -40, "a"), _closed("own", -44, "b", opened="2026-10-14T13:46:44+00:00"),
            _closed("own", -36, "c", opened="2026-10-14T13:48:43+00:00"),                 # exactly +120 s → same
            _closed("own", 10, "a", opened="2026-10-14T13:48:44+00:00"),                  # +121 s → new decision
            _closed("own", 5, "a", action="BUY"),                                          # other direction
            _closed("own", 5, "a", **{"symbol": "GOLD#"}),                                 # broker alias → same symbol
            _closed("own", 5, "a", **{"symbol": "EURUSD"})]
    clusters = group_decisions(rows)
    sizes = sorted(len(c) for c in clusters)
    assert sizes == [1, 1, 1, 4] and len(clusters) == 4


# ── 9 ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_entry_context_records_dxy_vix_10y_mtf_and_setup_score_for_scalps():
    from entry_context import context_from_signal
    from loss_postmortem import _quantitative_diff
    sig = {"action": "SELL", "strategy_engine": "hf_scalp_fast", "dxy": {"current_price": 103.2, "regime": "bullish"},
           "real_yield_10y": {"real_yield_10y": 1.85}, "mtf_tiers": {"alignment": {"dominant": "DOWN"}},
           "mtf_confluence": {"aligned": True}, "setup_score": {"score": 71.0}}
    ctx = context_from_signal(sig, {"VIXCLS": {"value": 18.4}, "DGS10": {"value": 4.12}})
    assert ctx["dxy"] == 103.2 and ctx["vix"] == 18.4 and ctx["y10y"] == 4.12 and ctx["real_yield_10y"] == 1.85
    assert ctx["mtf"] == {"dominant": "DOWN", "aligned": True, "confluence_aligned": True, "evaluated": True}
    assert ctx["aplus"]["setup_score"] == 71.0 and ctx["aplus"]["evaluated"] is False and ctx["engine"] == "hf_scalp_fast"
    assert context_from_signal({"action": "BUY", "mtf_tiers": {"alignment": {"dominant": "DOWN"}}})["mtf"]["aligned"] is False
    diff = _quantitative_diff({**sig, "entry_context": ctx}, {}, {})
    assert diff["dxy_at_entry"] == 103.2 and diff["vix_at_entry"] == 18.4 and diff["y10y_at_entry"] == 4.12
    assert diff["mtf_aligned_at_entry"] is True and diff["setup_score_at_entry"] == 71.0 and diff["aplus_evaluated_at_entry"] is False
    assert 'signal["entry_context"] = await build_entry_context(signal)' in _read("backend", "bot_runner.py")
    # v1.60.37 — the signal path reads the FRED cache only; the analytics worker keeps it warm
    ec = _read("backend", "entry_context.py")
    assert "get_macro_snapshot" not in ec and 'fred_cache.find_one({"_id": "snapshot"}' in ec
    assert "_warm_fred" in _read("backend", "background_loops.py")
    assert '"entry_context": _ectx' in _read("backend", "scalp", "engine.py")
