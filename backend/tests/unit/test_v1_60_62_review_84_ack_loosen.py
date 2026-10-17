"""v1.60.62 #84 — broker-confirmed stop looser than requested: adopt the ack as truth (v1.60.37 flag stays), send ONE
fenced corrective MODIFY_SL (never a loosen), alert if the correction is still loose, optional fenced FULL_CLOSE."""
import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

import ack_loosen as AL  # noqa: E402

pytestmark = pytest.mark.unit


def _run(c):
    return asyncio.get_event_loop().run_until_complete(c)


ACC = {"_id": ObjectId(), "user_id": "u", "symbol_specs": {"XAUUSD": {"point": 0.01, "stops_level_points": 30, "freeze_level_points": 0}}}
# tolerance = 30 points × 0.01 + 1 point = 0.31


def _trade(action="BUY", **extra):
    entry, sl, tp = (4100.0, 4095.0, 4110.0) if action == "BUY" else (4100.0, 4105.0, 4090.0)
    return {"_id": ObjectId(), "user_id": "u", "account_id": str(ACC["_id"]), "symbol": "XAUUSD", "action": action, "status": "open",
            "entry_price": entry, "stop_loss": sl, "original_stop_loss": sl, "take_profit": tp, "lot_size": 0.2, **extra}


# ── pure decision ─────────────────────────────────────────────────────────────────────────────────────────────
def test_tolerance_is_stops_level_plus_one_point():
    assert AL.tolerance_px(ACC, "XAUUSD") == pytest.approx(0.31)
    assert AL.tolerance_px({"_id": 1}, "XAUUSD") == pytest.approx(0.01)           # no specs: 1 point = pip/10 (gold pip 0.1)
    assert AL.tolerance_px({"_id": 1}, "EURUSD") == pytest.approx(0.00001)


def test_loosen_within_tolerance_is_no_action_buy_and_sell():
    buy = AL.decide(_trade("BUY"), ACC, {}, requested_sl=4101.0, confirmed_sl=4100.75, pending_reason="breakeven")
    sell = AL.decide(_trade("SELL"), ACC, {}, requested_sl=4099.0, confirmed_sl=4099.25, pending_reason="breakeven")
    assert buy["action"] == "none" and buy["why"] == "within broker tolerance" and buy["loosen_px"] == pytest.approx(0.25)
    assert sell["action"] == "none" and sell["why"] == "within broker tolerance"
    assert AL.decide(_trade("BUY"), ACC, {}, requested_sl=4101.0, confirmed_sl=4101.0, pending_reason="x")["why"] == "not loosened"
    tighter = AL.decide(_trade("BUY"), ACC, {}, requested_sl=4101.0, confirmed_sl=4101.5, pending_reason="x")   # broker tighter: fine
    assert tighter["action"] == "none" and tighter["loosen_px"] == 0


def test_loosen_beyond_tolerance_corrects_once_to_the_requested_stop_clamped_to_min_stop():
    buy = AL.decide(_trade("BUY"), ACC, {}, requested_sl=4101.0, confirmed_sl=4100.0, pending_reason="breakeven")
    assert buy["action"] == "correct" and buy["new_sl"] == 4101.0 and buy["extra_risk_r"] == pytest.approx(0.2)
    sell = AL.decide(_trade("SELL"), ACC, {}, requested_sl=4099.0, confirmed_sl=4100.0, pending_reason="breakeven")
    assert sell["action"] == "correct" and sell["new_sl"] == 4099.0
    # live price close to the requested stop → clamp to live − min_stop (BUY) / live + min_stop (SELL); still tighter than confirmed
    clamped = AL.decide(_trade("BUY"), ACC, {}, requested_sl=4101.0, confirmed_sl=4100.0, pending_reason="breakeven", live_price=4101.1)
    assert clamped["action"] == "correct" and clamped["new_sl"] == pytest.approx(4100.8)
    clamped_s = AL.decide(_trade("SELL"), ACC, {}, requested_sl=4099.0, confirmed_sl=4100.0, pending_reason="breakeven", live_price=4098.9)
    assert clamped_s["action"] == "correct" and clamped_s["new_sl"] == pytest.approx(4099.2)
    # the clamp would land AT/below the confirmed stop → no tighter request possible → alert, never a loosen
    no_room = AL.decide(_trade("BUY"), ACC, {}, requested_sl=4101.0, confirmed_sl=4100.0, pending_reason="breakeven", live_price=4100.2)
    assert no_room["action"] == "alert" and "new_sl" not in no_room


def test_correction_ack_still_loose_alerts_without_a_second_correction_and_flag_controls_close():
    tr = _trade("BUY", ack_loosen_correction={"intent_id": "abc", "new_sl": 4101.0})
    still = AL.decide(tr, ACC, {}, requested_sl=4101.0, confirmed_sl=4100.0, pending_reason=AL.CORRECTION_REASON)
    assert still["action"] == "alert" and still["extra_risk_r"] == pytest.approx(0.2)
    # flag ON but extra risk ≤ 0.25 R → alert only
    assert AL.decide(tr, ACC, {"ack_loosen_close_enabled": True}, requested_sl=4101.0, confirmed_sl=4100.0,
                     pending_reason=AL.CORRECTION_REASON)["action"] == "alert"
    # flag ON and extra risk > 0.25 R → close
    close = AL.decide(tr, ACC, {"ack_loosen_close_enabled": True}, requested_sl=4101.0, confirmed_sl=4099.0, pending_reason=AL.CORRECTION_REASON)
    assert close["action"] == "close" and close["extra_risk_r"] == pytest.approx(0.4)
    # flag OFF (default) never closes
    assert AL.decide(tr, ACC, {}, requested_sl=4101.0, confirmed_sl=4099.0, pending_reason=AL.CORRECTION_REASON)["action"] == "alert"
    assert AL.decide(tr, ACC, None, requested_sl=4101.0, confirmed_sl=4099.0, pending_reason=AL.CORRECTION_REASON)["action"] == "alert"


def test_corrective_sl_never_loosens():
    for action, req, con, live in (("BUY", 4101.0, 4100.0, None), ("BUY", 4101.0, 4100.0, 4100.1), ("SELL", 4099.0, 4100.0, None),
                                   ("SELL", 4099.0, 4100.0, 4099.95), ("BUY", 4099.0, 4100.0, None)):
        out = AL.corrective_sl(_trade(action), req, con, live, 0.30)
        if out is not None:
            assert (out > con) if action == "BUY" else (out < con), (action, req, con, live, out)


# ── route e2e: ack adopted as truth, correction fenced, alert, close ───────────────────────────────────────
def _ack_patches(br, db, acc):
    return [patch.object(br, "get_db", lambda: db), patch.object(br, "_account_by_token", AsyncMock(return_value=acc)),
            patch("command_fence.is_stale_ack", lambda *a, **k: False), patch("command_fence.ack_type_mismatch", lambda *a, **k: False),
            patch("command_fence.is_close_leg_ack", lambda *a, **k: False), patch("command_fence.is_duplicate_close_leg_ack", lambda *a, **k: False),
            patch("command_fence.ack_clears_pending", lambda *a, **k: True), patch.object(br.ws_manager, "broadcast", AsyncMock())]


def test_route_adopts_truth_corrects_once_then_alerts_then_closes_with_flag():
    from routes import bridge_routes as br
    db = FakeDb()
    acc = dict(ACC, status="connected", bridge_token_hash="x")
    db.accounts.rows.append(acc)
    db.bot_configs.rows.append({"user_id": "u", "account_id": str(acc["_id"]), "ack_loosen_close_enabled": False})
    tr = _trade("BUY", pending_modification={"type": "MODIFY_SL", "new_sl": 4101.0, "reason": "breakeven", "intent_id": "i1", "seq": 1}, command_seq=1)
    db.trades.rows.append(tr)
    tid = str(tr["_id"])
    ps = _ack_patches(br, db, acc)
    for p in ps:
        p.start()
    try:
        # 1) breakeven ack: broker says SL 4100.0 (requested 4101.0, 1.00 beyond a 0.31 tolerance)
        _run(br.modification_ack(br.BridgeModificationAck(bridge_token="t", trade_id=tid, type="MODIFY_SL", success=True,
                                                          new_sl=4101.0, confirmed_position_sl=4100.0, intent_id="i1")))
        row = db.trades.rows[0]
        assert row["stop_loss"] == 4100.0 and row["confirmed_stop_loss"] == 4100.0       # broker truth adopted, not refused
        assert "ack_loosened_stop" not in row         # v1.60.37 flag is about the PREVIOUS stop (4095 → 4100 tightened); #84 is vs the REQUESTED stop
        pm = row["pending_modification"]
        assert pm and pm["type"] == "MODIFY_SL" and pm["reason"] == AL.CORRECTION_REASON and pm["new_sl"] == 4101.0
        assert pm.get("intent_id") and pm.get("seq") == 2                              # fenced
        assert row["ack_loosen_correction"]["intent_id"] == pm["intent_id"]
        assert not db.ops_alerts.rows
        # 2) the correction's ack is STILL loose → one WARNING, no second correction, no close (flag OFF)
        _run(br.modification_ack(br.BridgeModificationAck(bridge_token="t", trade_id=tid, type="MODIFY_SL", success=True,
                                                          new_sl=4101.0, confirmed_position_sl=4099.0, intent_id=pm["intent_id"])))
        row = db.trades.rows[0]
        assert row["stop_loss"] == 4099.0 and row["pending_modification"] is None
        assert len(db.ops_alerts.rows) == 1
        al = db.ops_alerts.rows[0]
        assert al["kind"] == AL.ALERT_KIND and al["severity"] == "warning" and "stop wider than planned" in al["message"]
        assert "requested SL 4101.0 / broker SL 4099.0" in al["message"] and "0.4 R" in al["message"]
        assert row["ack_loosen_correction"]["outcome"] == "alert"
        # 3) flag ON on this account: same still-loose correction ack → fenced FULL_CLOSE via mandatory_close
        db.bot_configs.rows[0]["ack_loosen_close_enabled"] = True
        row["pending_modification"] = {"type": "MODIFY_SL", "new_sl": 4101.0, "reason": AL.CORRECTION_REASON, "intent_id": "i3", "seq": 3}
        row["ack_loosen_correction"]["outcome"] = None
        mc = AsyncMock(return_value="issued")
        with patch("close_commands.mandatory_close", mc):
            _run(br.modification_ack(br.BridgeModificationAck(bridge_token="t", trade_id=tid, type="MODIFY_SL", success=True,
                                                              new_sl=4101.0, confirmed_position_sl=4099.0, intent_id="i3")))
        assert mc.await_count == 1
        kw = mc.await_args.kwargs
        assert kw["reason"] == AL.CLOSE_REASON and kw["actor"] == "ack_loosen" and kw["account"] is acc
        assert db.trades.rows[0]["ack_loosen_correction"]["outcome"] == "close"
        assert len(db.ops_alerts.rows) == 1                                               # deduped on the trade
    finally:
        for p in ps:
            p.stop()


def test_route_no_action_within_tolerance_and_never_emits_a_loosen():
    from routes import bridge_routes as br
    db = FakeDb()
    acc = dict(ACC, status="connected")
    db.accounts.rows.append(acc)
    tr = _trade("SELL", pending_modification={"type": "MODIFY_SL", "new_sl": 4099.0, "reason": "breakeven", "intent_id": "i1", "seq": 1}, command_seq=1)
    db.trades.rows.append(tr)
    ps = _ack_patches(br, db, acc)
    for p in ps:
        p.start()
    try:
        _run(br.modification_ack(br.BridgeModificationAck(bridge_token="t", trade_id=str(tr["_id"]), type="MODIFY_SL", success=True,
                                                          new_sl=4099.0, confirmed_position_sl=4099.2, intent_id="i1")))
        row = db.trades.rows[0]
        assert row["stop_loss"] == 4099.2 and row["pending_modification"] is None and not db.ops_alerts.rows
        assert "ack_loosen_correction" not in row
    finally:
        for p in ps:
            p.stop()
    src = open(os.path.join(os.path.dirname(__file__), "..", "..", "ack_loosen.py"), encoding="utf-8").read()
    assert "tightened_sl(" in src and 'if d["action"] == "correct":' in src
    cfg_src = open(os.path.join(os.path.dirname(__file__), "..", "..", "models.py"), encoding="utf-8").read()
    assert "ack_loosen_close_enabled: bool = False" in cfg_src
