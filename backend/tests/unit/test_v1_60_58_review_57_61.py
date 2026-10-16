"""v1.60.58 — review of PR #107/#108: #57 external-deal decorator, #58 stale_feature_entry gated on the flag,
#59 STREAK_FIELDS projection, #60 cross-account cooldown R from original_stop_loss, #61 quiet window / friday_flat Telegram."""
import asyncio
import os
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from tests.unit.fake_mongo import FakeDb  # noqa: E402


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── #57 ──────────────────────────────────────────────────────────────────────────────────────────────

def test_57_every_bridge_route_is_bound_to_its_public_endpoint():
    from routes import bridge_routes as br
    table = {r.path: r.endpoint.__name__ for r in br.router.routes}
    assert table["/bridge/external-deal"] == "external_deal"
    assert table == {
        "/bridge/heartbeat": "heartbeat", "/bridge/candles": "receive_candles", "/bridge/ticks": "receive_ticks",
        "/bridge/dom": "receive_dom", "/bridge/poll-trades": "poll_trades", "/bridge/sync-complete": "sync_complete",
        "/bridge/modification-ack": "modification_ack", "/bridge/report": "report_trade", "/bridge/external-deal": "external_deal",
    }
    assert not [n for n in table.values() if n.startswith("_")], "a private helper is registered as an endpoint"


def test_57_external_deal_post_via_asgi_app_returns_200_and_updates_the_trade():
    import httpx
    import server
    from routes import bridge_routes as br

    db = FakeDb()
    acc_id, tid = ObjectId(), ObjectId()
    acc = {"_id": acc_id, "user_id": "u1", "account_type": "demo", "label": "demo"}
    db.accounts.rows.append(acc)
    db.trades.rows.append({"_id": tid, "user_id": "u1", "account_id": str(acc_id), "status": "open", "symbol": "XAUUSD",
                           "action": "BUY", "mt5_ticket": 777, "origin": "auto", "lot_size": 0.1, "entry_price": 4100.0})
    body = {"bridge_token": "tok", "mt5_ticket": 777, "deal_id": 9001, "deal_entry": "in", "symbol": "XAUUSD",
            "action": "BUY", "lots": 0.1, "price": 4100.0, "commission": -0.7, "magic": 901234}

    async def go():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            return await c.post("/api/bridge/external-deal", json=body)

    with patch.object(br, "get_db", lambda: db), patch.object(br, "_account_by_token", AsyncMock(return_value=acc)), \
            patch.object(br, "_bridge_ip_throttle", AsyncMock(return_value=None)), \
            patch.object(br, "_mark_deal_reconciled", AsyncMock(return_value=None)):
        r = _run(go())
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True and r.json().get("noop") == "ticket_already_tracked"
    assert db.broker_deals.rows and db.broker_deals.rows[0]["deal_id"] == 9001
    row = db.trades.rows[0]
    assert row.get("commission_entry") is not None, "the IN deal must stamp the entry commission (#53)"


# ── #58 / #59 ────────────────────────────────────────────────────────────────────────────────────────

def test_58_stale_feature_entry_is_gated_on_engine_live_features_flag():
    src = open(os.path.join(os.path.dirname(__file__), "..", "..", "ai_signals.py")).read()
    assert '"stale_feature_entry": bool(_live_on and (_pack_live or {}).get("stale_feature"))' in src
    assert '_live_on = bool((freeze_cfg or {}).get("engine_live_features_enabled"))' in src
    # flag OFF ⇒ the stale loss counts for the 2-in-4h guard exactly as before
    from streak_losses import classify_loss
    t = {"pnl": -40.0, "entry_price": 4100.0, "original_stop_loss": 4096.0, "exit_price": 4096.0, "lot_size": 0.1,
         "action": "BUY", "symbol": "XAUUSD"}
    assert classify_loss({**t, "stale_feature_entry": False})["counts"] is True
    assert classify_loss({**t, "stale_feature_entry": True})["counts"] is False


def test_59_streak_fields_project_stale_feature_entry():
    from streak_losses import STREAK_FIELDS
    assert STREAK_FIELDS.get("stale_feature_entry") == 1
    import anti_tilt
    import learning_pipeline
    assert "STREAK_FIELDS" in open(anti_tilt.__file__).read() and "STREAK_FIELDS" in open(learning_pipeline.__file__).read()


# ── #60 ──────────────────────────────────────────────────────────────────────────────────────────────

def test_60_breakeven_scratch_is_not_a_real_loss_across_accounts():
    from loss_cooldown import is_real_loss, trade_r_multiple
    scratch = {"entry_price": 4100.0, "original_stop_loss": 4096.0, "stop_loss": 4100.0,   # BE-moved stop sits at entry
               "exit_price": 4099.97, "action": "BUY", "pnl": -0.3}
    r = trade_r_multiple(scratch)
    assert r is not None and -0.1 < r < 0, r
    assert is_real_loss(scratch) is False
    loss = {**scratch, "exit_price": 4096.0, "pnl": -40.0}
    assert is_real_loss(loss) is True
    assert is_real_loss({"entry_price": 4100.0, "stop_loss": 4096.0, "exit_price": 4096.0, "action": "SELL"}) is False  # SELL +1R


# ── #61 ──────────────────────────────────────────────────────────────────────────────────────────────

def test_61_quiet_window_never_pauses_panic_or_protection():
    from close_watchdog import ea_quiet
    quiet_now = datetime(2025, 10, 16, 23, 50, tzinfo=timezone.utc)   # unknown offset (0) → inside 23:40–00:05
    acc = {"broker_utc_offset_sec": 0}
    assert ea_quiet(acc, quiet_now) is True
    assert ea_quiet(acc, quiet_now, "friday_flat") is True and ea_quiet(acc, quiet_now, "eod_flatten") is True
    for reason in ("panic", "protection", "pre_news_protect", "unprotected_position", "hard_drawdown", "late_fill_after_x"):
        assert ea_quiet(acc, quiet_now, reason) is False, reason
        assert ea_quiet({"ea_quiet_window": True}, quiet_now, reason) is False, reason


def test_61_mandatory_close_redispatches_panic_inside_quiet_window():
    from close_commands import mandatory_close
    now = datetime(2025, 10, 16, 23, 50, tzinfo=timezone.utc)
    db = FakeDb()
    tid = ObjectId()
    tr = {"_id": tid, "status": "open", "symbol": "XAUUSD", "account_id": "a1", "user_id": "u1", "mt5_ticket": 1,
          "close_requested": True, "close_command": {"key": "k0", "reason": "panic", "state": "requested"}, "pending_modification": {"type": "FULL_CLOSE", "reason": "panic",
                                                                                      "requested_at": "2025-10-16T23:00:00+00:00"},
          "close_redispatch": {"count": 0, "last_at": "2025-10-16T23:00:00+00:00"}}
    db.trades.rows.append(dict(tr))
    acc = {"broker_utc_offset_sec": 0}
    import close_commands as cc
    with patch.object(cc, "request_close", AsyncMock(return_value={"trades_marked_for_close": 1})):
        out = _run(mandatory_close(db, dict(tr), reason="panic", actor="t", now=now, account=acc))
    assert out == "reissued", out
    tr_ff = {**tr, "close_command": {"key": "k0", "reason": "friday_flat", "state": "requested"},
             "pending_modification": {**tr["pending_modification"], "reason": "friday_flat"}}
    with patch.object(cc, "request_close", AsyncMock(return_value={"trades_marked_for_close": 1})):
        out_ff = _run(mandatory_close(db, tr_ff, reason="friday_flat", actor="t", now=now, account=acc))
    assert out_ff == "quiet"


def test_61_friday_flat_close_notifies_after_an_earlier_tighten():
    import friday_flat as ff
    db = FakeDb()
    tid = ObjectId()
    db.trades.rows.append({"_id": tid, "user_id": "u1", "account_id": "a1", "status": "open", "symbol": "XAUUSD", "action": "BUY",
                           "friday_flat_at": "2025-10-17T19:00:00+00:00", "friday_flat_action": "tighten",
                           "pending_modification": {"type": "MODIFY_SL", "reason": "friday_flat"}})
    db.accounts.rows.append({"_id": ObjectId(), "ea_version": "1.60.0"})
    sent = []

    async def _notify(user_id, closed, tightened, actions):
        sent.append((closed, tightened, [a[1] for a in actions]))

    with patch.object(ff, "in_friday_flat_window", lambda cfg, now=None: {"in_window": True, "close_at": "x"}), \
            patch.object(ff, "_ea_supports_full_close", lambda acc: True), \
            patch("close_commands.mandatory_close", AsyncMock(return_value="issued")), \
            patch("notifier.notify_friday_flat", _notify), \
            patch.object(ff.ws_manager, "broadcast", AsyncMock()):
        stats = _run(ff.sweep_user(db, "u1", {"account_id": "a1", "friday_flat_mode": "close"}))
    assert stats["closed"] == 1
    assert sent == [(1, 0, ["close"])], sent
    # a re-issue of an already-closing trade stays silent (#49)
    db.trades.rows[0]["friday_flat_action"] = "close"
    sent.clear()
    with patch.object(ff, "in_friday_flat_window", lambda cfg, now=None: {"in_window": True, "close_at": "x"}), \
            patch.object(ff, "_ea_supports_full_close", lambda acc: True), \
            patch("close_commands.mandatory_close", AsyncMock(return_value="reissued")), \
            patch("notifier.notify_friday_flat", _notify), \
            patch.object(ff.ws_manager, "broadcast", AsyncMock()):
        _run(ff.sweep_user(db, "u1", {"account_id": "a1", "friday_flat_mode": "close"}))
    assert sent == []


@pytest.mark.parametrize("reason", ["panic", "protection"])
def test_61_watchdog_sweep_source_passes_reason_to_quiet_check(reason):
    src = open(os.path.join(os.path.dirname(__file__), "..", "..", "close_watchdog.py")).read()
    assert 'if mandatory and ea_quiet(acc, now, row.get("reason")):' in src
    from close_watchdog import reason_rank
    assert reason_rank(reason) <= 1
