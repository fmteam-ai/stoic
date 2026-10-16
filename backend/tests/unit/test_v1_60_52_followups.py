"""v1.60.52 — v1.60.50 review follow-ups (10 items) + optional H1 alignment gate for the HF scalp engines."""
import asyncio
import os
import sys
import time
from datetime import datetime, timedelta, timezone
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


def _src(*rel):
    return open(os.path.join(BACKEND, *rel)).read()


# ------------------------------------------------------------------------------------------ NEW: require_h1_alignment
def test_h1_alignment_gate_both_states_default_off_and_wired_for_scalp_engines_only():
    from mtf_check import h1_alignment
    from models import BotConfigUpdate
    assert BotConfigUpdate().require_h1_alignment is False
    against = {"action": "BUY", "scope": "hf_scalp", "mtf_tiers": {"alignment": {"dominant": "DOWN"}}}
    with_ = {"action": "SELL", "scope": "hf_scalp_fast", "mtf_tiers": {"alignment": {"dominant": "DOWN"}}}
    assert h1_alignment(against) == {"checked": True, "aligned": False, "dominant": "DOWN", "source": "mtf_tiers"}
    assert h1_alignment(with_)["aligned"] is True
    # an explicit mtf_gate wins over the tier-derived bias; MIXED / missing bias = not checked (fail-open)
    assert h1_alignment({**against, "mtf_gate": {"checked": True, "aligned": True, "htf_trend": "UP"}})["aligned"] is True
    assert h1_alignment({"action": "BUY", "mtf_tiers": {"alignment": {"dominant": "MIXED"}}})["checked"] is False
    assert h1_alignment({"action": "BUY"})["checked"] is False
    src = _src("bot_runner.py")
    blk = src[src.index("# v1.60.52 — optional per-account H1 alignment gate"):src.index("# iter-60 · Structure / Range / Fed-tone gates")]
    assert 'cfg.get("require_h1_alignment")' in blk and 'signal.get("scope") in _h1_scopes' in blk
    assert 'h1["checked"] and h1["aligned"] is False' in blk and "continue" in blk
    assert 'reason=f"H1 against trade — skipped' in blk and 'inc_intel_counter(user_id, "h1_align_block")' in blk
    page = open(os.path.join(FRONTEND, "pages", "BotConfig.jsx")).read()
    assert 'data-testid="require-h1-alignment-toggle"' in page and "require_h1_alignment: cfg.require_h1_alignment ?? false" in page


# ------------------------------------------------------------------------------------------ 1. direction cap lifecycle
def test_direction_cap_skips_paper_and_shadow_and_releases_in_finally():
    src = _src("bot_runner.py")
    blk = src[src.index("_udc_live = "):src.index('logger.info("Bot auto-execute user=%s')]
    assert '(target_account.get("mode") or "live") != "paper" and not shadow_only' in blk
    assert "if user_dir_cap > 0 and _udc_live:" in blk
    # exactly one release, in a finally that wraps engine.execute, the stale-quote skip and the blocked path
    assert blk.count("await _udc_release(db, _udc_res)") == 1
    fin = blk.index("        finally:")
    assert blk.index("trade_doc = await engine.execute(") < blk.index("except StaleQuoteError") < blk.index('if trade_doc.get("blocked")') < fin
    assert blk.index("await _udc_release(db, _udc_res)") > fin
    assert "_udc_release" in src[: src.index("async def _record_pulse")]       # helper swallows its own errors


# ------------------------------------------------------------------------------------------ 2. stale quotes
def test_stale_copy_is_never_served_past_the_age_limit_and_trading_callers_refuse():
    import market
    market._LAST_GOOD.clear()
    market._cache.clear()
    good = {"price": 4000.0, "bid": 4000.0, "ask": 4000.2, "change": 0, "change_pct": 0, "high": None, "low": None, "timestamp": "t"}

    async def dead():
        raise market.httpx.ConnectError("down")
    with patch.object(market, "_gold_quote", dead), patch.object(market, "QUOTE_RETRY_DELAY_SEC", 0):
        market._LAST_GOOD["XAUUSD"] = (time.time() - 55, good)
        q = _run(market.get_quote("XAUUSD"))
        assert q["stale"] is True and 54 <= q["stale_age_sec"] <= 56
        assert market._cache["quote:XAUUSD"][0] - time.time() <= 5.5, "stale copy TTL is clipped to the remaining age"
        market._LAST_GOOD["XAUUSD"] = (time.time() - 61, good)       # the limit passes while the copy is cached
        with pytest.raises(RuntimeError):
            _run(market.get_quote("XAUUSD"))                           # not served from the stale copy any more
        market._LAST_GOOD["XAUUSD"] = (time.time() - 30, good)
        _run(market.get_quote("XAUUSD"))
        with pytest.raises(market.StaleQuoteError):
            _run(market.get_quote("XAUUSD", allow_stale=False))        # cached stale copy refused by trading paths
    market._LAST_GOOD.clear()
    market._cache.clear()
    assert "get_quote(sym, allow_stale=False)" in _src("trigger_sweeper.py")
    assert "get_quote(payload.symbol, allow_stale=False)" in _src("routes", "trade_routes.py")
    srv = _src("server.py")
    assert "@app.exception_handler(StaleQuoteError)" in srv and "status_code=409" in srv[srv.index("_stale_quote_refused"):] \
        and '"code": "price_feed_stale"' in srv


# ------------------------------------------------------------------------------------------ 3. relabel
def test_relabel_never_replaces_valid_v2_m1_with_m15_and_requires_first_m15_bar_within_15_min():
    import deferred_shadow as ds
    db = FakeDb()
    epoch = 1_760_000_000
    sig = {"action": "HOLD", "entry_price": 4000.0, "stop_loss": 3995.0, "take_profit": 4010.0, "symbol": "XAUUSD"}
    ts = datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
    ids = [ObjectId(), ObjectId()]
    db.trade_decisions.rows += [
        {"_id": ids[0], "user_id": "u1", "status": "deferred", "symbol": "XAUUSD", "ts": ts, "signal": sig,
         "shadow": {"outcome": "tp_first", "r": 2.0, "timeframe": "M1", "label_version": 2, "direction": "BUY"}},   # valid v2 M1
        {"_id": ids[1], "user_id": "u1", "status": "deferred", "symbol": "XAUUSD", "ts": ts, "signal": sig,
         "shadow": {"outcome": "sl_first", "r": -1.0}},                                                              # v1
    ]
    m15 = [{"t": epoch + 900 * i, "o": 4000.0, "h": 4000.5, "l": 3999.5, "c": 4000.0} for i in range(100)]
    m15[1]["l"] = 3990.0                                                     # on M15 the verdict would flip to sl_first
    db.intraday_candles.rows.append({"user_id": "u1", "symbol": "XAUUSD", "timeframe": "M15", "bars": m15})
    out = _run(ds.rescore_legacy_shadows(db, "u1", limit=10, rescore_all=True))
    sh0 = db.trade_decisions.rows[0]["shadow"]
    assert sh0["outcome"] == "tp_first" and sh0["timeframe"] == "M1" and "coarse" not in sh0 and out["kept"] == 1
    assert db.trade_decisions.rows[1]["shadow"]["timeframe"] == "M15"       # the v1 row IS re-scored on M15
    # first M15 bar starts 2 h after the decision → unusable → pre-v2 row marked invalid, not scored
    db.trade_decisions.rows[1]["shadow"] = {"outcome": "sl_first", "r": -1.0}
    db.intraday_candles.rows[0]["bars"] = [{**b, "t": b["t"] + 7200} for b in m15]
    out = _run(ds.rescore_legacy_shadows(db, "u1", limit=10))
    sh1 = db.trade_decisions.rows[1]["shadow"]
    assert out["tagged_m15"] == 1 and sh1["invalid"] is True and "15 min" in sh1["invalid_reason"]
    assert _run(ds._m15_after(db, "u1", "XAUUSD", epoch, {})) == []
    assert len(_run(ds._m15_after(db, "u1", "XAUUSD", epoch + 7200 - 10, {}))) == 100


# ------------------------------------------------------------------------------------------ 4. failed close-leg ack
def test_failed_close_leg_ack_keeps_pending_modify_and_duplicates_stay_stale():
    from command_fence import ack_clears_pending, is_duplicate_close_leg_ack
    trade = {"close_requested": True, "close_idem_key": "ck1", "executed_intents": [],
             "pending_modification": {"type": "MODIFY_SL", "intent_id": "mod9"}}
    assert ack_clears_pending(trade, "ck1", success=False, close_leg=True) is False      # SL move still owed
    assert ack_clears_pending(trade, "ck1", success=True, close_leg=True) is True        # position gone → slot free
    assert ack_clears_pending(trade, "mod9", success=False, close_leg=False) is True     # own intent failed → consumed
    assert ack_clears_pending({**trade, "pending_modification": None}, "ck1", success=False, close_leg=True) is True
    assert is_duplicate_close_leg_ack(trade, "ck1") is False
    assert is_duplicate_close_leg_ack({**trade, "executed_intents": ["ck1"]}, "ck1") is True
    src = _src("routes", "bridge_routes.py")
    assert "_dup_close = is_duplicate_close_leg_ack(trade, payload.intent_id)" in src
    assert 'update = {"pending_modification": None} if ack_clears_pending(trade, payload.intent_id, bool(payload.success), _close_leg) else {}' in src


# ------------------------------------------------------------------------------------------ 5. original_lot_size
def test_streak_loss_reads_original_lot_size():
    from streak_losses import is_streak_loss, STREAK_FIELDS, _planned_r
    partial = {"symbol": "XAUUSD", "entry_price": 4000.0, "stop_loss": 3990.0, "lot_size": 0.01,
               "original_lot_size": 0.10, "pnl": -5.0}                 # planned risk on 0.10 lots = $100 → −0.05R
    assert is_streak_loss(partial) is False and abs(_planned_r(partial) + 0.05) < 0.01
    assert is_streak_loss({k: v for k, v in partial.items() if k != "original_lot_size"}) is True   # 0.01 lots → −0.5R
    assert "original_lot_size" in STREAK_FIELDS and "original_lot" not in STREAK_FIELDS


# ------------------------------------------------------------------------------------------ 6. preset_fields
def test_preset_response_lists_only_the_fields_the_preset_changed():
    import routes.bot_routes as br
    db = FakeDb()
    db.bot_configs.rows.append({"_id": ObjectId(), "user_id": "u1", "account_id": None, "risk_level": "low", "symbols": ["XAUUSD"]})

    async def no_cap(_db):
        return None
    with patch.object(br, "get_db", lambda: db), patch.object(br, "policy_risk_cap", no_cap):
        out = _run(br.apply_strategy_preset("scalper", None, user={"id": "u1"}))
    from strategy_presets import get_preset
    expected = sorted(set(get_preset("scalper")["config"]) | {"active_preset"})
    assert out["preset_fields"] == expected
    assert len(out["preset_fields"]) < len(out["config"]) - 10, "not the whole returned config"
    page = open(os.path.join(FRONTEND, "pages", "BotConfig.jsx")).read()
    assert "...(data.preset_fields || [])" in page and "...Object.keys(data.config || {})" not in page


# ------------------------------------------------------------------------------------------ 7. DXY fallback age
def test_dxy_snapshot_fetch_failure_fallback_respects_48h_limit():
    import macro.dxy as dxy
    db = FakeDb()

    async def boom():
        raise dxy.httpx.ConnectError("fred down")
    stale = {"_id": "latest", "current_price": 98.1, "regime": "WEAK",
             "fetched_at": (datetime.now(timezone.utc) - timedelta(hours=245)).isoformat()}
    db.dxy_cache.rows.append(stale)
    with patch.object(dxy, "get_db", lambda: db), patch.object(dxy, "_fetch_csv", boom):
        assert _run(dxy.get_dxy_snapshot()) is None
        db.dxy_cache.rows[0]["fetched_at"] = (datetime.now(timezone.utc) - timedelta(hours=20)).isoformat()
        out = _run(dxy.get_dxy_snapshot())
        assert out and out["current_price"] == 98.1 and out["regime"] == "WEAK" and "_id" not in out
        db.dxy_cache.rows[0].pop("fetched_at")
        assert _run(dxy.get_dxy_snapshot()) is None, "no timestamp = stale"


# ------------------------------------------------------------------------------------------ 8. safe_overlay asia OFF
def test_safe_overlay_keeps_explicit_asia_skip_off_but_still_tightens_everything_else():
    from strategy_presets import safe_overlay
    preset = {"asia_session_skip_xau": True, "sl_cooldown_enabled": True, "max_concurrent_trades": 5, "risk_level": "high"}
    out = safe_overlay({"asia_session_skip_xau": False, "sl_cooldown_enabled": False, "max_concurrent_trades": 2,
                        "risk_level": "low"}, preset)
    assert out["asia_session_skip_xau"] is False
    assert out["sl_cooldown_enabled"] is True and out["max_concurrent_trades"] == 2 and out["risk_level"] == "low"
    assert safe_overlay({"asia_session_skip_xau": None}, preset)["asia_session_skip_xau"] is True
    assert safe_overlay({}, preset)["asia_session_skip_xau"] is True
    assert safe_overlay({"asia_session_skip_xau": True}, {"asia_session_skip_xau": False})["asia_session_skip_xau"] is True


# ------------------------------------------------------------------------------------------ 9. nl apply enums + cap
def test_nl_apply_lowercases_enums_and_enforces_the_policy_risk_cap():
    import routes.nl_routes as nr
    from fastapi import HTTPException
    db = FakeDb()
    db.bot_configs.rows.append({"_id": ObjectId(), "user_id": "u1", "account_id": "A", "symbols": ["XAUUSD"], "risk_level": "low"})
    with patch.object(nr, "get_db", lambda: db):
        out = _run(nr.nl_strategy_apply({"compiled": {"risk_level": " Medium", "session_preference": "London",
                                                       "strategy_style": "Scalping", "symbols": ["xauusd"]},
                                         "target": "A", "preview": True}, user={"id": "u1"}))
    uf = out["update_fields"]
    assert uf["risk_level"] == "medium" and uf["session_preference"] == "london" and uf["strategy_style"] == "scalping"
    assert uf["active_preset"] == nr.STYLE_PRESET["scalping"]

    async def cap(_db):
        return 0.5
    with patch.object(nr, "get_db", lambda: db), patch("routes.bot_routes.policy_risk_cap", cap), \
            pytest.raises(HTTPException) as ei:
        _run(nr.nl_strategy_apply({"compiled": {"risk_level": "HIGH", "symbols": ["XAUUSD"]}, "target": "A"}, user={"id": "u1"}))
    assert ei.value.status_code == 409 and ei.value.detail["code"] == "risk_above_policy_cap"
    assert db.bot_configs.rows[0]["risk_level"] == "low", "nothing written"


# ------------------------------------------------------------------------------------------ 10. protector configs
def test_protector_skips_orphaned_per_account_configs_and_sorts_before_to_list():
    import position_protector as pp
    db = FakeDb()
    live_acct = ObjectId()
    db.accounts.rows.append({"_id": live_acct, "user_id": "u1"})
    ids = [ObjectId() for _ in range(3)]
    db.bot_configs.rows += [
        {"_id": ids[2], "user_id": "u1", "account_id": str(ObjectId()), "active": True},            # orphan (account deleted)
        {"_id": ids[0], "user_id": "u1", "account_id": None, "active": True},                      # default config
        {"_id": ids[1], "user_id": "u1", "account_id": str(live_acct), "active": False},            # paused, account exists
    ]
    seen = []

    async def fake_sweep(_db, user_id, cfg):
        seen.append(cfg["_id"])
        return 0
    with patch.object(pp, "sweep_user", fake_sweep):
        _run(pp.sweep_all(db))
    assert seen == [ids[0], ids[1]], "orphan skipped; visited in _id order"
    assert 'cursor.sort("_id", 1).to_list(length=200)' in _src("position_protector.py")
