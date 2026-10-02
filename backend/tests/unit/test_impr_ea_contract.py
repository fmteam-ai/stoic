"""impr-ea · backend side of the EA v1.58 execution contract.

Pure unit tests (no MongoDB, no network): poll-trades payload contract
(expires_at_ms / max deviation for OPEN commands only, manage_external for
adopted positions), scalp quote-freshness math with and without the EA's
sent_gmt_ms, and market.get_history broker-D1 preference.
"""
import asyncio
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _run(coro):
    return asyncio.run(coro)


# ───────────────────────── poll-trades contract ─────────────────────────
def _cursor(rows):
    cur = MagicMock()
    cur.to_list = AsyncMock(return_value=rows)
    return cur


def _poll(open_docs, close_docs, mod_docs, acc_extra=None):
    from routes import bridge_routes as br
    acc = {"_id": "acc1", "user_id": "u1", **(acc_extra or {})}
    db = MagicMock()
    db.trades.update_many = AsyncMock(return_value=MagicMock(modified_count=0))
    db.trades.find_one_and_update = AsyncMock(side_effect=list(open_docs) + [None])
    db.trades.find = MagicMock(side_effect=[_cursor(close_docs), _cursor(mod_docs)])
    with patch.object(br, "get_db", return_value=db), \
            patch.object(br, "_account_by_token", AsyncMock(return_value=acc)):
        return _run(br.poll_trades(br.PollRequest(bridge_token="tok")))


def _open_doc(**kw):
    d = {"_id": "t_open", "symbol": "EURUSD", "action": "BUY", "lot_size": 0.1,
         "entry_price": 1.1000, "stop_loss": 1.0980, "take_profit": 1.1040,
         "mt5_ticket": None, "status": "pending",
         "opened_at": datetime.now(timezone.utc).isoformat()}
    d.update(kw)
    return d


def test_open_command_carries_expiry_and_max_deviation(monkeypatch):
    monkeypatch.delenv("MAX_PENDING_OPEN_AGE_S", raising=False)
    monkeypatch.delenv("EXEC_MAX_ENTRY_DEVIATION_PIPS", raising=False)
    monkeypatch.delenv("EXEC_MAX_ENTRY_DEVIATION_SL_FRAC", raising=False)
    opened = datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc)
    resp = _poll([_open_doc(opened_at=opened.isoformat())], [], [],
                 acc_extra={"symbol_specs": {"EURUSD": {"point": 0.00001}}})
    item = resp["trades"][0]
    assert item["expires_at_ms"] == int((opened.timestamp() + 120) * 1000)
    # max(10 pips, 0.5 × 20-pip stop) = 10 pips = 0.0010
    assert item["max_deviation_price"] == pytest.approx(0.0010)
    assert item["max_deviation_points"] == 100
    assert "manage_external" not in item


def test_open_deviation_scales_with_stop_and_env(monkeypatch):
    monkeypatch.setenv("EXEC_MAX_ENTRY_DEVIATION_PIPS", "2")
    resp = _poll([_open_doc(symbol="XAUUSD", entry_price=2400.0,
                            stop_loss=2385.0)], [], [])
    item = resp["trades"][0]
    # 0.5 × $15 stop beats 2 gold pips ($0.20); no specs → no points
    assert item["max_deviation_price"] == pytest.approx(7.5)
    assert "max_deviation_points" not in item


def test_expiry_honours_max_pending_open_age_env(monkeypatch):
    monkeypatch.setenv("MAX_PENDING_OPEN_AGE_S", "30")
    opened = datetime.now(timezone.utc) - timedelta(seconds=5)
    item = _poll([_open_doc(opened_at=opened.isoformat())], [], [])["trades"][0]
    assert item["expires_at_ms"] == int((opened.timestamp() + 30) * 1000)


def test_close_and_modification_commands_never_carry_open_contract():
    close_ext = {"_id": "t_close", "symbol": "EURUSD", "action": "SELL",
                 "lot_size": 0.2, "entry_price": 1.1, "stop_loss": 1.11,
                 "take_profit": 1.09, "close_requested": True, "close_seq": 3,
                 "mt5_ticket": 555, "external_open": True, "origin": "manual",
                 "magic_number": 0}
    mod_ext = {"_id": "t_mod_ext", "symbol": "EURUSD", "mt5_ticket": 777,
               "magic_number": 4242, "origin": "other_ea",
               "pending_modification": {"type": "MODIFY_SL", "new_sl": 1.09,
                                        "intent_id": "i1", "seq": 1}}
    mod_bot = {"_id": "t_mod_bot", "symbol": "EURUSD", "mt5_ticket": 778,
               "magic_number": 901234, "origin": "auto",
               "pending_modification": {"type": "FULL_CLOSE",
                                        "intent_id": "i2", "seq": 2}}
    resp = _poll([], [close_ext], [mod_ext, mod_bot])
    (close_item,) = resp["trades"]
    for k in ("expires_at_ms", "max_deviation_price", "max_deviation_points"):
        assert k not in close_item
    assert close_item["manage_external"] is True
    mods = {m["trade_id"]: m for m in resp["modifications"]}
    assert mods["t_mod_ext"]["manage_external"] is True
    assert "manage_external" not in mods["t_mod_bot"]
    assert "expires_at_ms" not in mods["t_mod_ext"]


def test_pending_close_on_dispatched_ticket_is_not_an_open():
    doc = _open_doc(_id="t_pc", mt5_ticket=999, close_requested=True,
                    external_open=True)
    item = _poll([doc], [], [])["trades"][0]
    assert "expires_at_ms" not in item
    assert item["manage_external"] is True


@pytest.mark.parametrize("doc,expected", [
    ({"external_open": True}, True),
    ({"origin": "other_ea"}, True),
    ({"origin": "external"}, True),
    ({"magic_number": 0}, True),
    ({"magic_number": 12345}, True),
    ({"magic_number": 901234, "origin": "auto"}, False),
    # STOIC UI "manual" signals are still opened BY the EA (our magic)
    ({"origin": "manual"}, False),
    ({}, False),
    ({"adopted_via_external_deal": True, "external_open": False}, False),
])
def test_manage_external_rule(doc, expected):
    from routes.bridge_routes import _manage_external
    assert _manage_external(doc) is expected


def test_ticks_model_accepts_optional_sent_gmt_ms():
    from routes.bridge_routes import BridgeTicks
    assert BridgeTicks(bridge_token="t", symbol="EURUSD").sent_gmt_ms is None
    assert BridgeTicks(bridge_token="t", symbol="EURUSD",
                       sent_gmt_ms=123).sent_gmt_ms == 123


# ───────────────────────── scalp freshness math ─────────────────────────
TZ3 = 3 * 3600 * 1000          # EET broker (UTC+3) — broker-local tick clock


def test_clock_freshness_modes():
    from scalp.engine import clock_freshness
    assert clock_freshness(1000, None, None)["mode"] == "none"
    leg = clock_freshness(10_000, 9_000, None)
    assert leg["mode"] == "legacy" and leg["raw_transport_ms"] == 1000
    now = 1_700_000_000_000
    f = clock_freshness(now, now - 2700 + TZ3, now - 2500)
    assert f["mode"] == "gmt"
    assert f["transport_ms"] == 2500 and f["transport_ok"] is True
    # tick age on the EA's own clock: sent_gmt − (tick − TZ) = 200ms
    assert f["ea_tick_age_ms"] == pytest.approx(200)
    late = clock_freshness(now, now + TZ3 - 4000, now - 4000)
    assert late["transport_ok"] is False          # > MAX_BATCH_TRANSPORT_AGE_MS
    skew = clock_freshness(now, now + TZ3, now + 5000)
    assert skew["transport_ok"] is False          # EA clock 5s ahead
    assert skew["transport_ms"] == 0              # bounded, never negative


def _runner(sym="EURUSD"):
    from scalp.engine import ScalpRunner
    r = ScalpRunner("accImpr", "u1", sym)
    r._maybe_flush_ticks = MagicMock()
    return r


def _ingest(r, ticks, sent_at, sent_gmt=None):
    db = MagicMock()
    with patch("scalp.engine.permissions.maybe_refresh", MagicMock()), \
            patch("scalp.engine.strategy_select.maybe_refresh", MagicMock()):
        if sent_gmt is None:
            return _run(r.ingest(db, {"equity": 1000}, ticks, sent_at))
        return _run(r.ingest(db, {"equity": 1000}, ticks, sent_at,
                             sent_gmt_ms=sent_gmt))


def _batch(i, tick_utc_ms):
    return [{"tm": tick_utc_ms + TZ3, "b": 1.08 + i * 1e-5,
             "a": 1.08006 + i * 1e-5}]


def test_legacy_absorbs_steady_lag_but_gmt_mode_exposes_it():
    lag = 4000                                  # steady 4s transport lag
    legacy, gmt = _runner(), _runner()
    for i in range(12):
        now = int(time.time() * 1000)
        tick_utc = now - lag - 50              # tick 50ms old when sent
        out_l = _ingest(legacy, _batch(i, tick_utc), tick_utc + TZ3)
        out_g = _ingest(gmt, _batch(i, tick_utc), tick_utc + TZ3,
                        sent_gmt=now - lag)
    # legacy: the 4s lag became "clock drift" → batch looks trusted/fresh
    assert out_l["clock_mode"] == "legacy" and out_l["trusted"] is True
    assert legacy.state.broker_adjusted_age_ms() < 1000
    # new: transport measured directly → untrusted, quote age includes lag
    assert out_g["clock_mode"] == "gmt" and out_g["trusted"] is False
    assert out_g["transport_age_ms"] >= lag
    assert out_g["batch_fresh"] is False
    assert gmt.state.broker_adjusted_age_ms() >= lag
    assert gmt.state.gmt_clock is True


def test_gmt_mode_fresh_batch_passes_and_tight_drift_limit():
    from scalp import kill
    r = _runner()
    for i in range(12):
        now = int(time.time() * 1000)
        tick_utc = now - 150
        out = _ingest(r, _batch(i, tick_utc), tick_utc + TZ3,
                      sent_gmt=now - 100)
    assert out["trusted"] is True and out["batch_fresh"] is True
    assert out["ea_tick_age_ms"] == pytest.approx(50, abs=5)
    assert kill.drift_limit_ms(r.state) == kill.MAX_CLOCK_DRIFT_GMT_MS == 2000
    assert abs(kill.clock_drift_residual_ms(r.state.clock_drift_ms)) < 2000
    # legacy state keeps the 15s allowance
    assert kill.drift_limit_ms(_runner().state) == kill.MAX_CLOCK_DRIFT_MS


def test_gmt_mode_halts_on_broker_clock_error_over_two_seconds():
    from scalp import kill
    r = _runner()
    for i in range(12):
        now = int(time.time() * 1000)
        # broker clock 5s behind true UTC: ticks look 5s old on the EA clock
        tick_broker_utc = now - 5000
        _ingest(r, _batch(i, tick_broker_utc), tick_broker_utc + TZ3,
                sent_gmt=now - 50)
    h = kill.evaluate(r.state, r.cfg)
    assert h["status"] == "HALTED"
    assert any("clock drift excessive" in x for x in h["reasons"])


def test_switching_modes_resets_offset_population():
    from scalp.state import ScalpState
    st = ScalpState(0.0001)
    for _ in range(5):
        st.record_offset_sample(1000.0)
    st.set_clock_mode(True)
    assert len(st._offsets) == 0 and st.clock_drift_ms == 0.0
    st.set_clock_mode(True)                      # idempotent
    st.record_offset_sample(-TZ3)
    assert len(st._offsets) == 1


# ───────────────────────── market.get_history ─────────────────────────
def _d1_bars(n, end_ts=None, base=1.10):
    end_ts = int(end_ts or time.time()) // 86400 * 86400
    return [{"t": end_ts - (n - 1 - i) * 86400, "o": base, "h": base + 0.01,
             "l": base - 0.01, "c": base + 0.005, "v": 1000 + i}
            for i in range(n)]


def _public_rows(n=150):
    d0 = datetime(2026, 1, 1)
    return [{"date": (d0 + timedelta(days=i)).strftime("%Y-%m-%d"),
             "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 0}
            for i in range(n)]


@pytest.fixture
def mkt():
    import market
    market._cache.clear()
    market.USER_HISTORY_META.clear()
    yield market
    market._cache.clear()


def _db_with(doc):
    db = MagicMock()
    db.intraday_candles.find_one = AsyncMock(return_value=doc)
    return db


def test_get_history_prefers_users_broker_d1(mkt):
    db = _db_with({"bars": _d1_bars(200)})
    with patch("database.get_db", return_value=db), \
            patch.object(mkt, "_fx_history", AsyncMock(return_value=_public_rows())) as fx:
        hist = _run(mkt.get_history("EURUSD", user_id="u1"))
    assert len(hist) == 200 and fx.await_count == 0
    assert hist[-1]["high"] > hist[-1]["low"]          # real OHLC, not O=H=L=C
    q = db.intraday_candles.find_one.await_args[0][0]
    assert q == {"user_id": "u1", "symbol": "EURUSD", "timeframe": "D1"}
    assert mkt.USER_HISTORY_META[("EURUSD", "u1")]["provider"] == "broker_d1"


def test_get_history_without_user_id_unchanged(mkt):
    db = _db_with({"bars": _d1_bars(200)})
    with patch("database.get_db", return_value=db), \
            patch.object(mkt, "_fx_history", AsyncMock(return_value=_public_rows())), \
            patch.object(mkt, "_history_save_to_mongo", AsyncMock()):
        hist = _run(mkt.get_history("EURUSD"))
    assert len(hist) == 150
    db.intraday_candles.find_one.assert_not_called()


@pytest.mark.parametrize("doc", [
    None,                                            # no broker feed
    {"bars": _d1_bars(40)},                          # too few bars
    {"bars": _d1_bars(200, end_ts=time.time() - 10 * 86400)},   # stale feed
])
def test_get_history_falls_back_to_public_sources(mkt, doc):
    db = _db_with(doc)
    with patch("database.get_db", return_value=db), \
            patch.object(mkt, "_fx_history", AsyncMock(return_value=_public_rows())), \
            patch.object(mkt, "_history_save_to_mongo", AsyncMock()):
        hist = _run(mkt.get_history("EURUSD", user_id="u1"))
    assert len(hist) == 150


def test_broker_d1_rows_drop_malformed_and_dedupe():
    import market
    bars = _d1_bars(120)
    bars.append({"t": "x"})                            # malformed
    bars.append({**bars[-2], "c": 9.9})               # duplicate date → last wins
    bars.append({"t": bars[0]["t"] - 86400, "o": 1, "h": 0.5, "l": 1, "c": 1})  # h<l
    rows = market._broker_d1_rows(bars)
    assert len(rows) == 120
    assert rows[-1]["close"] == 9.9
    assert all("_t" not in r for r in rows)
    assert [r["date"] for r in rows] == sorted(r["date"] for r in rows)
