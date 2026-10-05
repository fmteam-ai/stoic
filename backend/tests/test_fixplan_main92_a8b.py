"""main92 review — step A8b (trading fixes from the main92 update).
P2   pre-trade price check compares the broker tick with the broker-tick entry (public quote only for public-quote signals).
P3   trade_of_day_cap default (1) is visible in the pulse reason when the config has no value.
P4   guardian caps default to the account's own id when the bot config has none.
P5   stop-less open trade fallback is a USD amount per trade symbol (no BTC pips on gold).
P6   EA broker offset snapped to 15 min (10799 → 10800).
P9   t_basis only "utc" when the offset is known; float timestamps are shifted.
P10  a failed sentiment model is cached for 2 min, not 1 h.
P11  heartbeat sightings written only when IP/terminal change.
H8   safety_guardian pip values are price-aware.
Pure unit tests (fake async db) — run with DB_NAME="".
"""
import asyncio
import inspect
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "unit"))
from fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── P2 ──────────────────────────────────────────────────────────────────────
def test_p2_preflight_price_prefers_broker_tick():
    import intraday_features as f

    async def tick(symbol, user_id, max_age_s=180):
        return 1.0852

    async def quote(symbol):
        return {"price": 1.0790}
    with patch.object(f, "broker_live_price", tick), patch("market.get_quote", quote):
        assert run(f.preflight_price("EURUSD", "u1", {"price_source": "broker_tick"})) == (1.0852, "broker_tick")


def test_p2_preflight_price_public_quote_only_for_public_quote_signals():
    import intraday_features as f

    async def no_tick(symbol, user_id, max_age_s=180):
        return None

    async def quote(symbol):
        return {"price": 1.0790}
    with patch.object(f, "broker_live_price", no_tick), patch("market.get_quote", quote):
        # signal priced from the broker tick, tick now stale → refuse (no like-for-like price)
        assert run(f.preflight_price("EURUSD", "u1", {"price_source": "broker_tick"})) == (0.0, "broker_tick_stale")
        # signal priced from the public quote → public quote is the right comparison
        assert run(f.preflight_price("EURUSD", "u1", {"price_source": "public_quote"})) == (1.0790, "public_quote")
        assert run(f.preflight_price("EURUSD", "u1", {})) == (1.0790, "public_quote")


def test_p2_both_pre_trade_checks_use_preflight_price():
    import execution
    import routes.trade_routes as tr
    assert "preflight_price(signal[\"symbol\"], user_id, signal)" in inspect.getsource(execution)
    src = inspect.getsource(tr.execute_signal)
    assert "preflight_price(signal[\"symbol\"], user[\"id\"], signal)" in src and "get_quote" not in src


# ── P3 ──────────────────────────────────────────────────────────────────────
def test_p3_trade_of_day_cap_default_is_flagged_in_pulse():
    import bot_runner
    src = inspect.getsource(bot_runner)
    assert 'trade_of_day_cap_default = cfg.get("trade_of_day_cap") is None' in src
    assert "default, not set on this config" in src


# ── P4 / P5 / H8 ────────────────────────────────────────────────────────────
def _account(**kw):
    return {"_id": "acc1", "id": "acc1", "mode": "live", "account_type": "standard",
            "equity": 10000.0, "balance": 10000.0, "free_margin": 9000.0, **kw}


def _signal(**kw):
    return {"symbol": "XAUUSD", "action": "BUY", "lot_size": 0.10,
            "entry_price": 2400.0, "stop_loss": 2395.0, "take_profit": 2410.0, **kw}


async def _macro_ok(*a, **k):
    return {"ok": True, "audit": []}


def _run_guardian(db, account, signal, cfg_account_id):
    import safety_guardian as sg
    with patch.object(sg, "macro_gate_evaluate", _macro_ok):
        return run(sg.audit_pre_trade(db=db, account=account, signal=signal,
                                      user_id="u1", cfg_account_id=cfg_account_id))


def test_p4_daily_loss_scoped_to_own_account_when_config_has_none():
    db = FakeDb()
    day = datetime.now(timezone.utc).isoformat()
    # a big loss on ANOTHER (paper) account of the same user must not block acc1
    db.trades.rows.append({"user_id": "u1", "account_id": "paper9", "status": "closed", "closed_at": day, "pnl": -5000.0})
    res = _run_guardian(db, _account(), _signal(), cfg_account_id=None)
    assert res["ok"], res["blocked_by"]
    # the same loss on acc1 itself blocks
    db.trades.rows[0]["account_id"] = "acc1"
    res = _run_guardian(db, _account(), _signal(), cfg_account_id=None)
    assert res["blocked_by"] == "daily_loss_cap"


def test_p5_stopless_trade_fallback_uses_its_own_symbol_in_usd():
    db = FakeDb()
    # a stop-less 0.01-lot BTC position: the fallback must be BTC's own pip
    # value × its own pips (not gold pips × BTC pip value)
    db.trades.rows.append({"user_id": "u1", "account_id": "acc1", "status": "open", "symbol": "BTCUSD",
                           "lot_size": 0.01, "entry_price": 60000.0, "stop_loss": 0, "sl_pips": 0})
    res = _run_guardian(db, _account(), _signal(), cfg_account_id="acc1")
    chk = next(a for a in res["audit"] if a["name"] == "total_open_risk_cap")
    assert chk["ok"], chk
    from pip_utils import pip_value_usd_per_lot, price_to_pips
    new_pips = price_to_pips("XAUUSD", 5.0)
    gold_pip = pip_value_usd_per_lot("XAUUSD", "standard", price=2400.0)
    expected = new_pips * gold_pip * 0.10 + max(new_pips, 100.0) * gold_pip * 0.01   # new trade + USD fallback × BTC lot
    assert abs(float(chk["value"].split("$")[1].split(" ")[0].replace(",", "")) - expected) < 1.0


def test_h8_guardian_pip_value_is_price_aware():
    import safety_guardian as sg
    src = inspect.getsource(sg.audit_pre_trade)
    assert 'pip_value_usd_per_lot(sym, account.get("account_type"), price=entry)' in src
    assert "price=t_entry or None" in src
    # USDCHF at 0.80: a pip is worth 12.50, not 10 → risk 25% higher
    from pip_utils import pip_value_usd_per_lot
    assert abs(pip_value_usd_per_lot("USDCHF", "standard", price=0.80) - 12.5) < 0.01


# ── P6 / P9 ─────────────────────────────────────────────────────────────────
def test_p6_ea_offset_snaps_to_15_minutes():
    from routes.bridge_routes import broker_offset_sec
    assert broker_offset_sec({"broker_time_info": {"server_gmt_offset_sec": 10799}}) == (10800, "ea_broker_time")
    assert broker_offset_sec({"broker_time_info": {"server_gmt_offset_sec": -17999}}) == (-18000, "ea_broker_time")
    assert broker_offset_sec({"broker_time_info": {"server_gmt_offset_sec": 10800}}) == (10800, "ea_broker_time")


def test_p9_t_basis_and_float_timestamps():
    import routes.bridge_routes as br
    src = inspect.getsource(br)
    assert '"t_basis": "utc" if offset_source != "unknown" else "broker_unknown"' in src
    assert "int(float(b[\"t\"])) - offset" in src


# ── P10 ─────────────────────────────────────────────────────────────────────
def test_p10_failed_sentiment_model_short_cache():
    import news
    assert news.SENTIMENT_FAILURE_TTL_S <= 300
    src = inspect.getsource(news)
    assert "SENTIMENT_FAILURE_TTL_S if model_failed else 3600" in src


# ── P11 ─────────────────────────────────────────────────────────────────────
def test_p11_heartbeat_sighting_written_only_on_change():
    from routes.bridge_routes import record_heartbeat_sighting
    from security_agent.events import request_ip

    class P:
        installation_id = "term-1"
        terminal_build = None
    db = FakeDb()
    acc = {"_id": "a1", "hb_sightings": [{"ip": "1.2.3.4", "terminal": "term-1", "at": "x"}]}
    db.accounts.rows.append(dict(acc))
    tok = request_ip.set("1.2.3.4")
    try:
        run(record_heartbeat_sighting(db, acc, P()))
        assert len(db.accounts.rows[0]["hb_sightings"]) == 1          # unchanged → no write
        request_ip.set("5.6.7.8")
        run(record_heartbeat_sighting(db, acc, P()))
        assert len(db.accounts.rows[0]["hb_sightings"]) == 2          # IP changed → appended
    finally:
        request_ip.reset(tok)
