"""Execution / risk-path review fixes — pure, no DB (AsyncMock / MagicMock).

bot_runner.py cannot be imported in an isolated unit env (it pulls the whole
LLM stack), so its small helpers are extracted via AST and exec'd, and the
fail-closed control flow is asserted structurally on the source.
"""
import ast
import asyncio
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BOT_RUNNER = os.path.join(BACKEND, "bot_runner.py")


def _bot_runner_src() -> str:
    with open(BOT_RUNNER, encoding="utf-8") as f:
        return f.read()


def _extract(names):
    """Exec selected top-level functions from bot_runner.py in isolation."""
    src = _bot_runner_src()
    tree = ast.parse(src)
    ns = {"datetime": datetime, "timezone": timezone, "timedelta": timedelta}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            exec(compile(ast.Module([node], []), BOT_RUNNER, "exec"), ns)
    return ns


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── 1 · guards match the broker-suffixed symbol via base_symbol ────────────
def test_sym_match_matches_symbol_or_base_symbol():
    ns = _extract({"_sym_match"})
    assert ns["_sym_match"]("XAUUSD") == {
        "$or": [{"symbol": "XAUUSD"}, {"base_symbol": "XAUUSD"}]}


def test_sl_cooldown_query_uses_base_symbol_or():
    ns = _extract({"_sym_match", "_on_sl_cooldown"})
    db = MagicMock()
    cursor = MagicMock()
    cursor.sort.return_value.limit.return_value.to_list = AsyncMock(return_value=[])
    db.trades.find.return_value = cursor
    _run(ns["_on_sl_cooldown"](db, "u1", "XAUUSD", 45, account_id="a1"))
    q = db.trades.find.call_args[0][0]
    assert q["$or"] == [{"symbol": "XAUUSD"}, {"base_symbol": "XAUUSD"}]
    assert "symbol" not in q and q["account_id"] == "a1"


def test_pyramid_streak_and_daily_cap_queries_use_sym_match_and_opened_at():
    src = _bot_runner_src()
    # no remaining plain-symbol filters on these guards
    assert '"symbol": sym,\n            "action": signal["action"],' not in src
    assert '"user_id": user_id, "symbol": sym, "action"' not in src
    i = src.index("tod_q = {")
    block = src[i:i + 400]
    assert "**_sym_match(sym)" in block
    assert '"opened_at": {"$gte"' in block and '"created_at"' not in block
    assert src.count("**_sym_match(sym)") >= 3


# ── 2 · risk engine + std clamp fail CLOSED; clamp skip is per-symbol ─────
def _except_body_after(src, marker):
    i = src.index(marker)
    j = src.index("except Exception as e:", i)
    return src[j:j + 700]


def test_risk_engine_exception_fails_closed():
    src = _bot_runner_src()
    body = _except_body_after(src, "rev = await risk_engine_evaluate(")
    assert "fail-open" not in body.split("\n\n")[0]
    assert "continue" in body.split("# iter-58")[0]


def test_std_contract_clamp_fails_closed_and_never_returns():
    src = _bot_runner_src()
    i = src.index("# iter-127b · FINAL hard risk clamp")
    j = src.index("# Phase-1 · VALUE-DRIVEN GATE", i)
    block = src[i:j]
    assert "return\n" not in block            # would drop every later symbol
    assert block.count("continue") >= 2       # min-lot skip + exception skip
    assert "fail-closed" in block


def test_clamp_is_inside_per_symbol_loop():
    tree = ast.parse(_bot_runner_src())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_process_user_account_locked")
    loop = next(n for n in ast.walk(fn)
                if isinstance(n, ast.For) and getattr(n.target, "id", "") == "sym")
    seg = ast.get_source_segment(_bot_runner_src(), loop)
    assert "FINAL hard risk clamp" in seg and "risk_engine_evaluate" in seg


# ── 3 · engine.execute receives risk_pct + strategy_class ──────────────────
def test_execute_signal_carries_risk_pct_and_strategy_class():
    src = _bot_runner_src()
    i = src.index("trade_doc = await engine.execute(")
    call = src[i:i + 2000]
    assert '"risk_pct":' in call and '"strategy_class": signal.get("strategy_class")' in call


# ── 7 · reconciler status guard + heartbeat mismatch skips reconcile ──────
def test_reconcile_account_update_has_status_guard():
    import trade_reconciler as tr
    db = MagicMock()
    old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    cur = MagicMock()
    cur.to_list = AsyncMock(return_value=[
        {"_id": "t1", "status": "open", "mt5_ticket": 111, "opened_at": old}])
    db.trades.find.return_value = cur
    db.trades.update_one = AsyncMock(return_value=MagicMock(matched_count=0))
    db.accounts.update_one = AsyncMock()
    ws = MagicMock(); ws.broadcast = AsyncMock()
    with patch.object(tr, "get_db", return_value=db), patch.object(tr, "ws_manager", ws):
        out = _run(tr.reconcile_account("65f000000000000000000001", [], source="t"))
    flt = db.trades.update_one.await_args_list[0][0][0]
    assert flt == {"_id": "t1", "status": {"$in": ["open", "pending"]}}
    # already closed concurrently → not counted, no WS push
    assert out["closed_count"] == 0
    ws.broadcast.assert_not_awaited()


def test_heartbeat_ticket_mismatch_skips_reconcile_not_empties_tickets():
    with open(os.path.join(BACKEND, "routes", "bridge_routes.py"), encoding="utf-8") as f:
        src = f.read()
    i = src.index("ticket_count_mismatch = (")
    block = src[i:i + 3500]
    assert "tickets = []  # force orphan sweep" not in src
    assert "if ticket_count_mismatch:" in block
    assert '"skipped": "ticket_count_mismatch"' in block
    assert "tickets and not ticket_count_mismatch" in block


# ── 8 · stale pending OPEN orders are cancelled, not executed ──────────────
def test_cancel_stale_pending_opens_filter_targets_only_undispatched_opens():
    from routes import bridge_routes as br
    db = MagicMock()
    db.trades.update_many = AsyncMock(return_value=MagicMock(modified_count=2))
    cutoff = br._stale_pending_open_cutoff_iso()
    n = _run(br._cancel_stale_pending_opens(db, "acc1", cutoff))
    assert n == 2
    flt, upd = db.trades.update_many.await_args[0]
    assert flt["mt5_ticket"] is None and flt["close_requested"] == {"$ne": True}
    assert flt["opened_at"] == {"$lt": cutoff} and flt["status"] == "pending"
    assert upd["$set"]["status"] == "cancelled"
    assert upd["$set"]["close_reason"] == "stale_pending"


def test_max_pending_open_age_env(monkeypatch):
    from routes import bridge_routes as br
    monkeypatch.setenv("MAX_PENDING_OPEN_AGE_S", "300")
    assert br._max_pending_open_age_s() == 300.0
    monkeypatch.delenv("MAX_PENDING_OPEN_AGE_S")
    assert br._max_pending_open_age_s() == 120.0


# ── 10 · floor-to-step sizing, reject below min lot ────────────────────────
def test_floor_and_scale_lot_never_round_up():
    from risk import floor_lot, scale_lot
    assert floor_lot(0.129) == 0.12
    assert floor_lot(0.3) == 0.3                 # float noise absorbed
    assert floor_lot(0.0099) == 0.0
    assert scale_lot(0.05, 0.5) == 0.02          # 0.025 → 0.02 (old: 0.03)
    assert scale_lot(0.01, 0.5) == 0.0           # old code: max(0.01, …) = 0.01
    assert scale_lot(1.0, 0.333) == 0.33
    for lot in (0.01, 0.07, 0.13, 1.37):
        for s in (0.1, 0.33, 0.5, 0.77, 0.99):
            assert scale_lot(lot, s) <= lot * s + 1e-12


def test_compute_lot_floors_and_rejects_below_min_lot():
    from risk import compute_lot_for_account, get_profile
    p = {**get_profile("medium"), "risk_pct": 1.0}
    # $1000 × 1% = $10; XAU SL 15.0 → 150 pips × $10 = $1500/lot → 0.00667 lots.
    # The 0.01 minimum risks $15 = exactly the C5 1.5× tolerance → allowed.
    r = compute_lot_for_account(account={"equity": 1000.0, "account_type": "standard"},
                                symbol="XAUUSD", entry_price=4100.0, stop_loss=4115.0,
                                confidence_pct=70, profile=p)
    assert r["lot_size"] == 0.01
    # $500 → $5 budget; the minimum lot would risk 3× the budget → reject.
    r = compute_lot_for_account(account={"equity": 500.0, "account_type": "standard"},
                                symbol="XAUUSD", entry_price=4100.0, stop_loss=4115.0,
                                confidence_pct=70, profile=p)
    assert r["sizing_valid"] is False and r["lot_size"] == 0.0
    assert r["method"] == "rejected_min_lot_risk"
    # $10,000 → $100 / $1500 = 0.0667 → floor 0.06 (nearest would be 0.07)
    r = compute_lot_for_account(account={"equity": 10000.0, "account_type": "standard"},
                                symbol="XAUUSD", entry_price=4100.0, stop_loss=4115.0,
                                confidence_pct=70, profile=p)
    assert r["lot_size"] == 0.06
    assert r["actual_risk_usd"] <= r["risk_amount_usd"]


def test_compute_lot_respects_broker_volume_step():
    from risk import compute_lot_for_account, get_profile
    p = {**get_profile("medium"), "risk_pct": 1.0}
    r = compute_lot_for_account(
        account={"equity": 10000.0, "account_type": "standard",
                 "volume_step": 0.05, "volume_min": 0.05},
        symbol="XAUUSD", entry_price=4100.0, stop_loss=4115.0,
        confidence_pct=70, profile=p)
    assert r["lot_size"] == 0.05


# ── 11 · execution: SL/TP side + heartbeat / quote fail closed ─────────────
def test_sl_tp_side_block():
    from execution import sl_tp_side_block
    assert sl_tp_side_block({"action": "BUY", "entry_price": 100, "stop_loss": 99,
                             "take_profit": 102}) is None
    assert sl_tp_side_block({"action": "SELL", "entry_price": 100, "stop_loss": 101,
                             "take_profit": 98}) is None
    assert sl_tp_side_block({"action": "BUY", "entry_price": 100, "stop_loss": 101,
                             "take_profit": 102})["blocked"] == "invalid_sl_tp_side"
    assert sl_tp_side_block({"action": "BUY", "entry_price": 100, "stop_loss": 99,
                             "take_profit": 98})["blocked"] == "invalid_sl_tp_side"
    assert sl_tp_side_block({"action": "SELL", "entry_price": 100, "stop_loss": 99,
                             "take_profit": 98})["blocked"] == "invalid_sl_tp_side"
    assert sl_tp_side_block({"action": "SELL", "entry_price": 100, "stop_loss": 101,
                             "take_profit": 100})["blocked"] == "invalid_sl_tp_side"
    # legs ≤ 0 are not checked here (presence is enforced elsewhere)
    assert sl_tp_side_block({"action": "BUY", "entry_price": 100, "stop_loss": 0,
                             "take_profit": 0}) is None


def test_parse_hb_age_handles_naive_and_raises_on_garbage():
    from execution import _parse_hb_age_sec
    naive = (datetime.now(timezone.utc) - timedelta(seconds=30)).replace(tzinfo=None)
    assert 25 < _parse_hb_age_sec(naive.isoformat()) < 60
    assert 25 < _parse_hb_age_sec(naive) < 60
    with pytest.raises((TypeError, ValueError)):
        _parse_hb_age_sec("not-a-date")


def test_execution_fail_closed_paths_present():
    with open(os.path.join(BACKEND, "execution.py"), encoding="utf-8") as f:
        src = f.read()
    assert '"blocked": "heartbeat_unparseable"' in src
    assert '"blocked": "quote_unavailable"' in src
    i = src.index("age = _parse_hb_age_sec(hb)")
    assert "pass" not in src[i:i + 400]


# ── 12 · suggested SL only tightens, never crosses entry ───────────────────
def test_suggested_sl_valid():
    ns = _extract({"_suggested_sl_valid"})
    v = ns["_suggested_sl_valid"]
    assert v("BUY", 100, 95, 97) is True
    assert v("BUY", 100, 95, 101) is False      # above entry → instant stop
    assert v("BUY", 100, 95, 94) is False       # looser
    assert v("SELL", 100, 105, 103) is True
    assert v("SELL", 100, 105, 99) is False
    assert v("SELL", 100, 105, 106) is False
    assert v("BUY", None, 95, 97) is False


def test_trade_eval_rejects_wrong_side_quantile_stop():
    from prob_forecast import trade_eval
    # q10 above entry for a BUY (distribution shifted up) → no suggestion
    fc = {"quantile_levels": [0.1, 0.5, 0.9], "quantile_values": [101.0, 103.0, 105.0],
          "last": 100.0}
    out = trade_eval("XAUUSD", "BUY", 100.0, 95.0, 104.0, fc)
    assert out["suggested_sl"] is None
    fc2 = {"quantile_levels": [0.1, 0.5, 0.9], "quantile_values": [97.0, 100.0, 103.0],
           "last": 100.0}
    out2 = trade_eval("XAUUSD", "BUY", 100.0, 95.0, 104.0, fc2)
    assert out2["suggested_sl"] == 97.0
    fc3 = {"quantile_levels": [0.1, 0.5, 0.9], "quantile_values": [95.0, 97.0, 99.0],
           "last": 100.0}
    out3 = trade_eval("XAUUSD", "SELL", 100.0, 105.0, 96.0, fc3)
    assert out3["suggested_sl"] is None     # q90 99 < entry for a SELL


# ── misc · items 6, 9, 13, 14 ──────────────────────────────────────────────
def test_pip_size_any_jpy_pair():
    from pip_utils import pip_size, pip_value_usd_per_lot
    for s in ("CHFJPY", "CADJPY-ECN", "NZDJPY.r", "USDJPY"):
        assert pip_size(s) == 0.01
    assert pip_size("EURUSD") == 0.0001
    assert abs(pip_value_usd_per_lot("USDCHF", "standard", price=0.8) - 12.5) < 1e-9
    assert abs(pip_value_usd_per_lot("USDJPY", "standard", price=150) - 6.6667) < 1e-3
    assert pip_value_usd_per_lot("EURUSD", "standard", price=1.1) == 10.0


def test_risk_engine_notional_uses_fx_contract():
    from risk_engine import _notional
    assert abs(_notional("EURUSD-ECN", 1.0, 1.1) - 110_000.0) < 1e-6
    assert abs(_notional("XAUUSD", 1.0, 4000.0) - 400_000.0) < 1e-6
    assert abs(_notional("USDJPY", 1.0, 150.0) - 100_000.0) < 1e-6


def test_circuit_breaker_fails_closed_without_equity():
    import circuit_breakers as cb
    db = MagicMock()
    db.trades.find.return_value.to_list = AsyncMock(return_value=[])
    db.bot_configs.update_one = AsyncMock()
    out = _run(cb.check_and_trip(db, "u1", {"account_id": "a1"}, [{"equity": 0}]))
    assert out["tripped"] is True and out["kind"] == "no_equity"
    db.bot_configs.update_one.assert_not_awaited()   # transient, not persisted


def test_crypto_orders_blocked_reason(monkeypatch):
    from crypto_bridge.ccxt_engine import orders_blocked_reason
    monkeypatch.setenv("BINANCE_LIVE_ENABLED", "false")
    assert orders_blocked_reason({"exchange_id": "binance", "live": True})
    assert orders_blocked_reason({"exchange_id": "binance", "testnet": True}) is None
    assert "no sandbox" in orders_blocked_reason({"exchange_id": "kraken", "testnet": True})
    monkeypatch.setenv("BINANCE_LIVE_ENABLED", "true")
    assert orders_blocked_reason({"exchange_id": "kraken", "live": True}) is None
    assert "no sandbox" in orders_blocked_reason({"exchange_id": "binanceus", "testnet": True})
