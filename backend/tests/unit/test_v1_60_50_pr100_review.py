"""v1.60.50 — PR #100 review fixes (NO-GO items)."""
import asyncio
import os
import sys
import time
from datetime import datetime, timezone
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


# ----------------------------------------------------------------------------------------------- 1. Asia gold skip
def test_asia_gold_skip_is_a_second_window_and_user_end_time_is_not_stretched():
    from bot_runner import asia_gold_skip_active
    from agents.execution_optimizer_agent import off_hours_window, DEFAULT_OFF_HOURS_END
    from models import BotConfigUpdate
    t = lambda h, m=0, d=14: datetime(2026, 10, d, h, m, tzinfo=timezone.utc)  # noqa: E731 (Wed 14 Oct)
    assert asia_gold_skip_active(t(0, 0)) and asia_gold_skip_active(t(6, 59)) and not asia_gold_skip_active(t(7, 0))
    assert not asia_gold_skip_active(datetime(2026, 10, 17, 3, tzinfo=timezone.utc))      # weekend handled elsewhere
    assert DEFAULT_OFF_HOURS_END == "06:00" and BotConfigUpdate().gold_off_hours_end_utc == "06:00"
    assert off_hours_window({"gold_off_hours_end_utc": "23:00", "asia_session_skip_xau": True}) == (21 * 60, 23 * 60)
    assert BotConfigUpdate().asia_session_skip_xau is True
    src = open(os.path.join(BACKEND, "bot_runner.py")).read()
    assert 'cfg.get("asia_session_skip_xau", True) is not False and sym.upper() == "XAUUSD" and asia_gold_skip_active()' in src
    page = open(os.path.join(FRONTEND, "pages", "BotConfig.jsx")).read()
    assert 'data-testid="asia-gold-skip-toggle"' in page and 'data-testid="user-direction-cap-input"' in page


# ----------------------------------------------------------------------------------------------- 2. stale quotes
def test_stale_quote_has_max_age_is_cached_briefly_and_refused_by_trading_paths():
    import market
    market._LAST_GOOD.clear()
    market._cache.clear()

    async def dead():
        raise market.httpx.ConnectError("down")
    good = {"price": 4000.0, "bid": 4000.0, "ask": 4000.2, "change": 0, "change_pct": 0, "high": None, "low": None, "timestamp": "t"}
    market._LAST_GOOD["XAUUSD"] = (time.time() - 30, good)
    calls = {"n": 0}

    async def counting_dead():
        calls["n"] += 1
        raise market.httpx.ConnectError("down")
    with patch.object(market, "_gold_quote", counting_dead), patch.object(market, "QUOTE_RETRY_DELAY_SEC", 0):
        q = _run(market.get_quote("XAUUSD"))
        assert q["stale"] is True and q["price"] == 4000.0 and 29 <= q["stale_age_sec"] <= 31
        q2 = _run(market.get_quote("XAUUSD"))                 # served from the brief stale cache — no new attempts
        assert q2["stale"] is True and calls["n"] == market.QUOTE_ATTEMPTS
        with pytest.raises(market.StaleQuoteError):
            _run(market.get_quote("XAUUSD", allow_stale=False))
    market._cache.clear()
    market._LAST_GOOD["XAUUSD"] = (time.time() - 61, good)      # older than the 60 s limit → no fallback at all
    with patch.object(market, "_gold_quote", dead), patch.object(market, "QUOTE_RETRY_DELAY_SEC", 0), pytest.raises(RuntimeError):
        _run(market.get_quote("XAUUSD"))
    market._LAST_GOOD.clear()
    market._cache.clear()
    for rel, needle in (("trade_manager.py", "get_quote(symbol, allow_stale=False)"),
                        ("execution.py", 'get_quote(signal["symbol"], allow_stale=False)'),
                        ("execution.py", "get_quote(sym, allow_stale=False)"),
                        ("ai_signals.py", "get_quote(symbol, allow_stale=False)"),
                        ("bot_runner.py", "except StaleQuoteError as _sq:")):
        assert needle in open(os.path.join(BACKEND, rel)).read(), (rel, needle)


# ----------------------------------------------------------------------------------------------- 3. direction cap
class _UniqueColl:
    """FakeDb collection wrapper enforcing the unique (user, symbol, action, slot) index."""

    def __init__(self, coll):
        self.c = coll

    def __getattr__(self, n):
        return getattr(self.c, n)

    async def insert_one(self, doc, session=None):
        for r in self.c.rows:
            if all(r.get(k) == doc.get(k) for k in ("user_id", "symbol", "action", "slot")):
                raise RuntimeError("E11000 duplicate key error")
        return await self.c.insert_one(doc)


def test_user_direction_cap_reservation_is_atomic_counts_live_only_and_releases():
    import user_direction_cap as udc
    db = FakeDb()
    db.user_direction_reservations = _UniqueColl(db.user_direction_reservations)
    db.trades.rows += [
        {"user_id": "u1", "symbol": "XAUUSD", "action": "BUY", "status": "open", "origin": "auto", "mode": "paper"},
        {"user_id": "u1", "symbol": "XAUUSD", "action": "BUY", "status": "open", "origin": "auto", "shadow": True},
    ]
    kw = dict(user_id="u1", base_symbol="XAUUSD", action="BUY", cap=1, symbol_match_q="XAUUSD")

    async def race():
        return await asyncio.gather(udc.reserve_user_direction(db, account_id="A", **kw),
                                    udc.reserve_user_direction(db, account_id="B", **kw))
    (ra, occ_a), (rb, occ_b) = _run(race())
    assert (ra is None) != (rb is None), "exactly one of two concurrent accounts gets the slot"
    assert occ_a == 0 or occ_b == 0, "paper / shadow trades never count"
    winner = ra or rb
    _run(udc.release_user_direction(db, winner))
    rid, _ = _run(udc.reserve_user_direction(db, account_id="C", **kw))
    assert rid is not None, "released slot is available again"
    db.trades.rows.append({"user_id": "u1", "symbol": "XAUUSD", "action": "BUY", "status": "open", "origin": "auto", "mode": "live"})
    _run(udc.release_user_direction(db, rid))
    assert _run(udc.reserve_user_direction(db, account_id="C", **kw)) == (None, 1)
    src = open(os.path.join(BACKEND, "bot_runner.py")).read()
    assert src.index("reserve_user_direction(") < src.index("trade_doc = await engine.execute(")
    assert src.count("await _udc_release(db, _udc_res)") >= 2


# ----------------------------------------------------------------------------------------------- 4. relabel pagination
def test_rescore_paginates_keeps_original_rescored_from_and_never_downgrades_v2():
    import deferred_shadow as ds
    db = FakeDb()
    epoch = 1_760_000_000
    sig = {"action": "HOLD", "entry_price": 4000.0, "stop_loss": 3995.0, "take_profit": 4010.0, "symbol": "XAUUSD"}
    ts = datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
    ids = [ObjectId() for _ in range(3)]
    db.trade_decisions.rows += [
        {"_id": ids[0], "user_id": "u1", "status": "deferred", "symbol": "XAUUSD", "ts": ts, "signal": sig,
         "shadow": {"outcome": "sl_first", "r": -1.0, "timeframe": "M1"}},                                  # v1
        {"_id": ids[1], "user_id": "u1", "status": "deferred", "symbol": "XAUUSD", "ts": ts, "signal": sig,
         "shadow": {"outcome": "tp_first", "r": 2.0, "timeframe": "M1", "label_version": 2, "direction": "BUY"}},  # valid v2
        {"_id": ids[2], "user_id": "u1", "status": "deferred", "symbol": "XAUUSD", "ts": ts, "signal": sig,
         "shadow": {"outcome": "sl_first", "r": -1.0}},                                                     # v1, M15 only
    ]
    bars = [{"t": epoch + 60 * i, "o": 4000.0, "h": 4000.5, "l": 3999.5, "c": 4000.0} for i in range(1500)]
    bars[2]["h"] = 4011.0
    db.intraday_candles.rows.append({"user_id": "u1", "symbol": "XAUUSD", "timeframe": "M1", "bars": bars})
    # first page of 1 → id cursor
    out1 = _run(ds.rescore_legacy_shadows(db, "u1", limit=1, rescore_all=True))
    assert out1["visited"] == 1 and out1["next_after_id"] == ids[0]
    out2 = _run(ds.rescore_legacy_shadows(db, "u1", limit=5, rescore_all=True, after_id=out1["next_after_id"]))
    assert out2["visited"] == 2 and out2["next_after_id"] == ids[2]
    sh0 = db.trade_decisions.rows[0]["shadow"]
    assert sh0["outcome"] == "tp_first" and sh0["rescored_from"]["outcome"] == "sl_first"
    # a second --all pass must NOT overwrite the ORIGINAL rescored_from
    _run(ds.rescore_legacy_shadows(db, "u1", limit=5, rescore_all=True))
    assert db.trade_decisions.rows[0]["shadow"]["rescored_from"]["outcome"] == "sl_first"
    # valid v2 whose tape expired is kept, never downgraded
    db.intraday_candles.rows.clear()
    out = _run(ds.rescore_legacy_shadows(db, "u1", limit=5, rescore_all=True))
    assert out["kept"] >= 1 and db.trade_decisions.rows[1]["shadow"]["outcome"] == "tp_first"
    assert not db.trade_decisions.rows[1]["shadow"].get("invalid")
    # pre-v2 row with only M15 bars → re-scored on M15 with the corrected direction, flagged coarse
    db.trade_decisions.rows[2]["shadow"] = {"outcome": "sl_first", "r": -1.0}
    m15 = [{"t": epoch + 900 * i, "o": 4000.0, "h": 4000.5, "l": 3999.5, "c": 4000.0} for i in range(100)]
    m15[1]["h"] = 4011.0
    db.intraday_candles.rows.append({"user_id": "u1", "symbol": "XAUUSD", "timeframe": "M15", "bars": m15})
    _run(ds.rescore_legacy_shadows(db, "u1", limit=5))
    sh2 = db.trade_decisions.rows[2]["shadow"]
    assert sh2["outcome"] == "tp_first" and sh2["timeframe"] == "M15" and sh2["coarse"] is True and sh2["direction"] == "BUY"
    script = open(os.path.join(os.path.dirname(BACKEND), "scripts", "relabel_deferred_shadows.py")).read()
    assert "after_id=after" in script and 'out.get("next_after_id")' in script


# ----------------------------------------------------------------------------------------------- 5 / 6. frontend
def test_strategies_preview_resets_on_target_change_and_bot_config_sends_preset_fields():
    page = open(os.path.join(FRONTEND, "pages", "Strategies.jsx")).read()
    assert "useEffect(() => { setDeployPreview(null); }, [applyTarget, compiled]);" in page
    assert "confirm_multi: multi && Boolean(deployPreview)" in page
    bc = open(os.path.join(FRONTEND, "pages", "BotConfig.jsx")).read()
    assert "presetTouchedRef.current = new Set([...(presetTouchedRef.current || []), ...Object.keys(data.config || {})])" in bc
    assert "for (const f of presetTouchedRef.current) if (f in fullPayload && !(f in payload)) payload[f] = fullPayload[f];" in bc


# ----------------------------------------------------------------------------------------------- 7. apply validation
def test_strategy_apply_validates_enums_and_requires_step_up_on_live_risk_raise():
    import routes.nl_routes as nr
    from fastapi import HTTPException
    db = FakeDb()
    db.bot_configs.rows.append({"_id": ObjectId(), "user_id": "u1", "account_id": "A", "symbols": ["XAUUSD"], "risk_level": "low"})
    with patch.object(nr, "get_db", lambda: db), pytest.raises(HTTPException) as ei:
        _run(nr.nl_strategy_apply({"compiled": {"risk_level": "yolo", "symbols": ["XAUUSD"]}, "target": "A"}, user={"id": "u1"}))
    assert ei.value.status_code == 422
    with patch.object(nr, "get_db", lambda: db), pytest.raises(HTTPException) as ei:
        _run(nr.nl_strategy_apply({"compiled": {"risk_level": "low", "symbols": ["XAUUSD"], "session_preference": "mars"}, "target": "A"}, user={"id": "u1"}))
    assert ei.value.status_code == 422
    called = {}

    async def live(db_, uid, aid, owns=None):
        return True

    async def step_up(db_, user, request, action):
        called["action"] = action
        raise HTTPException(status_code=401, detail="step_up_required")
    with patch.object(nr, "get_db", lambda: db), patch("routes.bot_routes._live_context", live), \
            patch("step_up.require_step_up", step_up), pytest.raises(HTTPException) as ei:
        _run(nr.nl_strategy_apply({"compiled": {"risk_level": "high", "symbols": ["XAUUSD"]}, "target": "A"}, user={"id": "u1"}))
    assert ei.value.status_code == 401 and called["action"] == "risk_raise"
    assert db.bot_configs.rows[0]["risk_level"] == "low", "nothing written before step-up"


# ----------------------------------------------------------------------------------------------- 8. scratches
def test_streak_loss_uses_original_stop_and_confirmed_breakeven_only():
    from streak_losses import is_streak_loss
    moved = {"symbol": "XAUUSD", "entry_price": 4000.0, "stop_loss": 4000.05, "original_stop_loss": 3990.0,
             "lot_size": 0.01, "pnl": -0.5}                     # stop at BE: risk vs current stop ≈ 0
    assert is_streak_loss(moved) is False                      # −0.05R against the ORIGINAL stop
    assert is_streak_loss({**moved, "pnl": -5.0}) is True
    be_rule = {"symbol": "XAUUSD", "entry_price": 4000.0, "stop_loss": 3990.0, "lot_size": 0.01, "pnl": -8.0,
               "scalp_exit_rule": "breakeven_0_5r"}
    assert is_streak_loss(be_rule) is True                     # BE requested but never confirmed → real loss
    assert is_streak_loss({**be_rule, "breakeven_set": True}) is False


# ----------------------------------------------------------------------------------------------- 9. close leg ack
def test_close_leg_ack_outranks_pending_modify():
    from command_fence import is_close_leg_ack, is_stale_ack
    trade = {"close_requested": True, "close_idem_key": "ck1",
             "pending_modification": {"type": "MODIFY_SL", "intent_id": "mod9"}}
    assert is_close_leg_ack(trade, "ck1") and not is_close_leg_ack(trade, "mod9") and not is_close_leg_ack(trade, None)
    assert is_stale_ack(trade, "ck1") is True, "the raw fence still says stale — the route must bypass it"
    src = open(os.path.join(BACKEND, "routes", "bridge_routes.py")).read()
    assert "_close_leg = is_close_leg_ack(trade, payload.intent_id)" in src
    assert "if not _close_leg and is_stale_ack(trade, payload.intent_id):" in src
    assert "if not _close_leg and ack_type_mismatch(trade, payload.type):" in src


# ----------------------------------------------------------------------------------------------- 10. low
def test_low_items_regime_cache_key_tick_offset_and_paused_protector():
    assert "_ck = (user_id, (cfg or {}).get(\"account_id\"))" in open(os.path.join(BACKEND, "market_regime.py")).read()
    eng = open(os.path.join(BACKEND, "scalp", "engine.py")).read()
    assert '"broker_offset_sec": off,' in eng and "async def _store_tick_batch" in eng
    ds = open(os.path.join(BACKEND, "deferred_shadow.py")).read()
    assert 'off = batch.get("broker_offset_sec")' in ds
    pp = open(os.path.join(BACKEND, "position_protector.py")).read()
    assert '"account_id": {"$nin": [None]}, "pre_news_protect_enabled": {"$ne": False}' in pp
