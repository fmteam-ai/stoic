"""Fix plan step B2 — safety guards (R2, R5, R6, R7, R8, B6, R14).
R2   daily trade cap counts trades by `opened_at` (the field trades actually carry).
R5   std-contract risk clamp FAILS CLOSED: an error or missing input blocks that symbol and the loop continues.
R6   execution: no heartbeat / unreadable heartbeat / no live quote block the order.
R7   probabilistic forecast stop is only applied when it lies strictly between the stop and the entry.
R8   Guardian counts pending and stop-less trades; daily loss is per account, falls back to the user, every origin.
B6   manual execute passes the account id to the engine and refuses when price moved too far.
R14  bot routes: no 500 on malformed ids (source guards present).
Pure unit tests (fake async db, no Mongo) — run with DB_NAME="".
"""
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit



def test_r2_daily_cap_counts_opened_at():
    import bot_runner
    src = inspect.getsource(bot_runner)
    i = src.index("if trade_of_day_cap > 0:")
    block = src[i:i + 700]
    assert '"opened_at": {"$gte"' in block and '"created_at"' not in block


def test_r5_risk_clamp_fails_closed_and_continues():
    import bot_runner
    src = inspect.getsource(bot_runner)
    i = src.index("_clamp_error = None")
    block = src[i:i + 3600]
    assert "except Exception as e:  # noqa: BLE001\n            _clamp_error" in block
    assert 'await inc_intel_counter(user_id, "risk_clamp_block")' in block and block.count("continue") >= 2
    assert 'price=float(_ep)' in block                          # price-aware pip value (USDCHF & crosses do not block)


def test_r7_forecast_stop_bounded_by_entry():
    from prob_forecast import trade_eval
    levels = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]

    def fc(values):
        return {"quantile_levels": levels, "quantile_values": values, "last": 100.0}
    # BUY: q10 ABOVE entry (bullish band) → must NOT suggest a stop at/above entry
    r = trade_eval("XAUUSD", "BUY", 100.0, 95.0, 110.0, fc([101.0 + i for i in range(9)]))
    assert r is not None and r["suggested_sl"] is None
    # BUY: q10 between stop and entry → tighter stop allowed
    assert trade_eval("XAUUSD", "BUY", 100.0, 95.0, 110.0, fc([97.0 + i * 0.5 for i in range(9)]))["suggested_sl"] == 97.0
    # SELL: q90 BELOW entry → none; q90 between entry and stop → suggested
    assert trade_eval("XAUUSD", "SELL", 100.0, 105.0, 90.0, fc([85.0 + i for i in range(9)]))["suggested_sl"] is None
    assert trade_eval("XAUUSD", "SELL", 100.0, 105.0, 90.0, fc([99.0 + i * 0.5 for i in range(9)]))["suggested_sl"] == 103.0


def test_r6_missing_heartbeat_and_missing_quote_block():
    import execution
    src = inspect.getsource(execution.MT5BridgeEngine)
    i = src.index('hb = fresh.get("last_heartbeat")')
    block = src[i:i + 1500]
    assert 'if not hb:\n                    return {"blocked": "stale_heartbeat"' in block           # never heartbeated → blocked
    assert 'except (TypeError, ValueError):\n                    return {"blocked": "stale_heartbeat"' in block   # unreadable → blocked
    j = src.index("if not (px > 0):")
    assert '"blocked": "quote_unavailable"' in src[j:j + 500]                                      # no quote → blocked


def test_r8_guardian_counts_pending_stopless_and_daily_loss_without_account():
    import safety_guardian as sg
    src = inspect.getsource(sg.audit_pre_trade)
    assert '"status": {"$in": ["open", "pending"]}' in src
    assert "stopless_count" in src and "new_sl_usd_per_lot" in src   # main92 P5: USD fallback per trade symbol
    i = src.index("dl_match = ")
    assert '"origin"' not in src[i:i + 400] and "if cfg_account_id:\n        dl_match[\"account_id\"]" in src


def test_b6_manual_execute_price_check_and_account_scope():
    import routes.trade_routes as tr
    src = inspect.getsource(tr.execute_signal)
    assert '"code": "entry_deviation"' in src and '"code": "quote_unavailable"' in src
    assert "deviation > 0.5 * stop_dist" in src and "through_stop" in src
    assert 'cfg_account_id=str(account["_id"])' in src


def test_r14_bot_routes_guard_object_ids():
    import routes.bot_routes as br
    src = inspect.getsource(br)
    for line in [ln for ln in src.splitlines() if "ObjectId(" in ln and "parse_object_id" not in ln and "is_valid" not in ln]:
        assert 'user["id"]' in line or "try" in src[max(0, src.index(line) - 120):src.index(line)] or "ObjectId(user_id)" in line, line
