"""v1.60.33 — items 7-10 of the 14 Oct review.
7  never loosen a stop (legacy TP1 / let_winners_run path moved SELL stops from entry−costs back to raw entry)
8  scalp single TP is no longer re-split by the MTF TP1 pip cap
9  frontend: chunk-load error after a deploy → auto-reload once / "New version available"
10 FRED: an EMPTY cached snapshot is never "fresh"
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def _trade(**kw):
    t = {"_id": ObjectId(), "user_id": "u1", "symbol": "XAUUSD", "action": "SELL", "engine": None, "scope": "hf_scalp_fast",
         "entry_price": 4000.0, "stop_loss": 3999.75, "sl_pips": 400, "tp_pips": [100, 200, 200], "lot_size": 0.05,
         "original_lot_size": 0.05, "mt5_ticket": 1, "status": "open", "mode": "demo", "spread_pips": 2.5,
         "be_target_sl": 3999.75, "scalp_exit_rule": "breakeven_0_5r",
         "opened_at": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()}
    t.update(kw)
    return t


# ── 7 ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_tightened_sl_only_accepts_a_tighter_stop():
    from trade_manager import tightened_sl
    sell = {"action": "SELL", "stop_loss": 3999.75}
    assert tightened_sl(sell, 4000.0) is None            # the 15:13 incident: raw entry is ABOVE entry−costs on a SELL
    assert tightened_sl(sell, 3999.5) == 3999.5
    assert tightened_sl(sell, 3999.75) is None            # equal is not tighter
    buy = {"action": "BUY", "stop_loss": 4000.25}
    assert tightened_sl(buy, 4000.0) is None
    assert tightened_sl(buy, 4000.5) == 4000.5
    assert tightened_sl({"action": "BUY", "stop_loss": None}, 1.0) == 1.0   # no stop yet → anything is protection


def test_legacy_tp1_path_never_moves_a_sell_stop_back_up_to_raw_entry():
    """Reproduces 14 Oct 15:13: scalp BE already put the SELL stop at entry−costs (3999.75); price then reached the
    legacy +100-pip TP1 and the old path wrote MODIFY_SL new_sl=entry (4000.0) — a LOOSER stop."""
    import trade_manager as tm
    for cfg in ({"let_winners_run": True}, {}):
        db = FakeDb()
        t = _trade()
        db.trades.rows.append(t)
        quote = AsyncMock(return_value={"price": 4000.0 - 0.1 * 120})          # +120 pips, past TP1 (100)
        with patch.object(tm, "get_db", lambda: db), patch.object(tm, "get_quote", quote), \
                patch.object(tm, "ws_manager") as ws, patch("adaptive_exits.manage_exits", AsyncMock()):
            ws.broadcast = AsyncMock()
            _run(tm._manage_one_trade(t, cfg))
        row = db.trades.rows[0]
        pm = row.get("pending_modification") or {}
        assert pm.get("new_sl") is None, (cfg, pm)                             # no loosening write
        assert row["be_target_sl"] == 3999.75                                   # the protective target is untouched
        if cfg.get("let_winners_run"):
            assert row["tp1_closed"] is True and pm == {}                       # tier progressed, nothing sent to the EA
        else:
            assert pm["type"] == "PARTIAL_CLOSE" and pm["new_volume"] == 0.03   # partial close still banks half


def test_legacy_tp1_path_still_moves_the_stop_when_it_tightens():
    import trade_manager as tm
    db = FakeDb()
    t = _trade(action="BUY", stop_loss=3996.0, be_target_sl=None, scalp_exit_rule=None, scope="breakout_m15")
    db.trades.rows.append(t)
    quote = AsyncMock(return_value={"price": 4000.0 + 0.1 * 120})
    with patch.object(tm, "get_db", lambda: db), patch.object(tm, "get_quote", quote), patch.object(tm, "ws_manager") as ws:
        ws.broadcast = AsyncMock()
        _run(tm._manage_one_trade(t, {"let_winners_run": True}))
    pm = db.trades.rows[0]["pending_modification"]
    assert pm["type"] == "MODIFY_SL" and pm["new_sl"] == 4000.0 == db.trades.rows[0]["be_target_sl"]


def test_every_sl_write_in_the_manager_goes_through_the_guard():
    src = _read("backend", "trade_manager.py")
    assert '"new_sl": round(entry, 5)' not in src and "def tightened_sl(" in src
    assert "tightened_sl(trade, new_sl) is not None" in src                         # scalp BE path
    assert "be_sl = tightened_sl(trade, round(entry, 5))" in src                    # legacy TP1 paths
    br = _read("backend", "routes", "bridge_routes.py")
    assert "combo_new_sl = tightened_sl(trade, float(combo_new_sl))" in br           # Tier-1 combo follow-up


# ── 8 ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_scalp_engines_skip_the_mtf_tp_pip_caps():
    src = _read("backend", "ai_signals.py")
    i = src.index("tp1_dist = tp2_dist = tp3_dist = tp_dist_price")
    block = src[i:i + 900]
    assert "if engine not in SCALP_ENGINES:" in block
    assert block.index("if engine not in SCALP_ENGINES:") < block.index("MTF_TP1_MAX_PIPS")
    # the three caps are all inside the guarded block
    for cap in ("MTF_TP1_MAX_PIPS", "MTF_TP2_MAX_PIPS", "MTF_TP3_MAX_PIPS"):
        line = next(ln for ln in block.splitlines() if cap in ln and "min(" in ln)
        assert line.startswith("        "), line                                    # indented under the guard


# ── 9 ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_frontend_reloads_once_on_chunk_load_errors_then_offers_new_version_reload():
    app = _read("frontend", "src", "App.jsx")
    assert "dynamically imported module" in app and "Loading chunk" in app
    assert 'sessionStorage.getItem(RELOADED_KEY) !== window.location.href' in app and "window.location.reload()" in app
    reb = _read("frontend", "src", "components", "RouteErrorBoundary.jsx")
    assert "New version available" in reb and 'data-testid="new-version-reload"' in reb
    assert "dynamically imported module" in reb


# ── 10 ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_empty_cached_fred_snapshot_is_not_fresh_and_is_refetched(monkeypatch):
    import macro.fred as fred
    now = datetime.now(timezone.utc).isoformat()
    assert fred._fresh({"fetched_at": now, "payload": {"series": {}}}, 6) is False
    assert fred._fresh({"fetched_at": now, "payload": {}}, 6) is False
    assert fred._fresh({"fetched_at": now, "payload": {"series": {"DFF": {"value": 4.1}}}}, 6) is True
    db = FakeDb()
    db.fred_cache.rows.append({"_id": "snapshot", "fetched_at": now, "payload": {"series": {}, "fetched_at": now}})
    monkeypatch.setattr(fred, "get_db", lambda: db)
    rows = [("2026-09-01", 4.0), ("2026-10-10", 4.1)]
    with patch.object(fred, "fetch_series", AsyncMock(return_value=rows)), patch.object(fred, "transport", lambda: "fred_api"):
        out = _run(fred.get_macro_snapshot())
    assert out["stale"] is False and set(out["series"]) == set(fred.SERIES)       # refetched instead of serving {}
    assert db.fred_cache.rows[0]["payload"]["series"]
