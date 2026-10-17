"""EA v1.65 candidate — scope: new_tp on MODIFY_SL + M1 bar stream for the scalp symbol. Nothing else.

Guards: (1) order send / close / fencing functions are byte-identical to 1.64 (frozen hashes); (2) capability flags
make 1.64 and 1.65 coexist (new_tp only sent to modify_tp_v1 EAs; 1.65 works without new_tp); (3) M1 ingestion
dedupes, drops the forming bar, keeps 26 h, reports gaps; (4) the deferred-shadow replay prefers EA M1 bars."""
import hashlib
import os
import re
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytestmark = pytest.mark.unit
ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
EA = os.path.join(ROOT, "static", "EmergentTradingBridge.mq5")
SRC = open(EA, encoding="utf-8", errors="replace").read()

# sha256[:16] of each function body as shipped in EA 1.64 — the 1.65 scope must not touch them
FROZEN_1_64 = {
    "void ExecuteTrade(": "b39bd9b5d2bfb205", "void ApplyFullClose(": "4a2711d2c3192ad4",
    "void ClosePosition(": "ef44480f27e7d4cd", "void ApplyPartialClose(": "d1c89b29ad1ad206",
    "bool IntentDone(": "456743a7ed5b66ef", "void MarkIntentDone(": "21f32e9ac587042d",
    "bool NlCloseAdmitted(": "8ded91c9091285b6", "void SweepJournal(": "39907a973cbb1a30",
    "void SendHeartbeat(": "7d9dbadecdf45f04",
}


def _body(name: str) -> str:
    i = SRC.index("\n" + name)
    return SRC[i:SRC.index("\n}\n", i) + 3]


def test_ea_version_is_current_and_structure_ok():
    # v1.60.61 #81 — the shipped EA is 1.66 (1.65 + history backfill); the 1.65 scope guards below still hold
    assert '#property version   "1.66"' in SRC and '#define EA_CLIENT_VERSION "1.66"' in SRC
    for fe in ("pages/Accounts.jsx", "components/EaVersionStrip.jsx"):
        assert 'LATEST_EA_VERSION = "1.66"' in open(os.path.join(ROOT, "..", "frontend", "src", fe), encoding="utf-8").read()
    from ea_capabilities import latest_ea_version
    assert latest_ea_version() == "1.66"


@pytest.mark.parametrize("name,digest", sorted(FROZEN_1_64.items()))
def test_order_send_close_and_fencing_functions_unchanged_from_1_64(name, digest):
    assert hashlib.sha256(_body(name).encode()).hexdigest()[:16] == digest, f"{name} changed — out of 1.65 scope"


def test_modify_sl_new_tp_is_optional_and_acked():
    assert 'ExtractDouble(obj, "\\"new_tp\\":", 0)' in SRC
    assert "ApplyModifySL(trade_id, ticket, new_sl, intent, seq, new_tp);" in SRC
    sig = "void ApplyModifySL(string trade_id, long ticket, double new_sl,\n                   string intent = \"\", long seq = 0, double new_tp = 0)"
    assert sig in SRC                                  # default 0 → servers that never send new_tp keep the 1.64 path
    body = _body("void ApplyModifySL(")
    assert "req.tp       = current_tp;" in body and "if (new_tp > 0) {" in body and "if (adj_tp > 0) req.tp = adj_tp;" in body
    assert body.index("req.tp       = current_tp;") < body.index("if (new_tp > 0) {")
    assert '"confirmed_position_tp\\":%.5f' in body and "requested_tp" in body
    assert body.count("OrderSend(") == 1               # still exactly one SLTP request


def test_m1_stream_is_batched_on_the_slow_lane_after_heartbeat():
    assert "input bool   M1StreamEnabled        = true;" in SRC and "input int    M1StreamSeconds        = 60;" in SRC
    timer = _body("void OnTimer(")
    assert timer.index("SendHeartbeat();") < timer.index("SendM1Candles();")
    assert "TimeCurrent() - _last_m1_sent >= M1StreamSeconds" in timer
    sender = _body("void SendM1CandlesFor(")
    assert "PERIOD_M1" in sender and '\\"timeframe\\":\\"M1\\"' in sender and "(M1StreamBars > 120) ? 120" in sender
    assert SRC.count('"/api/bridge/candles"') == 3       # M15 sender + M1 sender + 1.66 history backfill, same endpoint


def test_capability_flags_let_1_64_and_1_65_coexist():
    from ea_capabilities import capabilities_for, live_gate
    assert {"modify_tp_v1", "m1_stream_v1"} <= capabilities_for("1.65")
    assert not {"modify_tp_v1", "m1_stream_v1"} & capabilities_for("1.64")
    assert "command_fencing_v1" in capabilities_for("1.64")      # 1.64 stays fully LIVE-capable (instant rollback)
    g = live_gate({"account_type": "live", "ea_version": "1.64", "ea_attested": True})
    assert g is None or g.get("code") not in ("EA_VERSION_UNKNOWN", "EA_CAPABILITY_BELOW_MIN")
    src = open(os.path.join(ROOT, "routes", "bridge_routes.py"), encoding="utf-8").read()
    poll = src.split("async def poll_trades(")[1].split("\n@router")[0]
    assert '_send_tp = "modify_tp_v1" in _ea_caps(acc.get("ea_version"))' in poll
    assert '"new_tp": m.get("new_tp") if _send_tp else None,' in poll


def test_ack_adopts_broker_confirmed_tp_when_new_tp_was_requested():
    src = open(os.path.join(ROOT, "routes", "bridge_routes.py"), encoding="utf-8").read()
    assert "confirmed_position_tp: float | None = None" in src
    ack = src.split("async def modification_ack(")[1].split("\n@router")[0]
    assert 'if payload.confirmed_position_tp and _pm.get("new_tp") and payload.success:' in ack
    assert 'update["take_profit"] = float(payload.confirmed_position_tp)' in ack
    assert 'update["confirmed_take_profit"]' in ack


def test_m1_ingestion_dedupes_drops_forming_bar_keeps_26h_and_reports_gaps():
    from routes.bridge_routes import gap_count, retention_bars, split_forming_bar, validate_bars
    now = int(datetime.now(timezone.utc).timestamp()) // 60 * 60
    raw = [{"t": now - 60 * k, "o": 1.0, "h": 1.1, "l": 0.9, "c": 1.05, "v": 3} for k in (5, 4, 3, 1, 0)]
    raw.append(dict(raw[1]))                                   # duplicate bar → deduped
    raw.append({"t": now - 120, "o": "x", "h": 1, "l": 1, "c": 1})   # invalid → dropped
    bars, dropped = validate_bars(raw, "M1")
    assert [b["t"] for b in bars] == [now - 300, now - 240, now - 180, now - 60, now] and dropped == 1
    closed, forming = split_forming_bar(bars, "M1", now + 10)
    assert forming == 1 and closed[-1]["t"] == now - 60        # the bar that opened at `now` is still forming
    assert gap_count(closed, "M1") == 1                         # now-120 is missing
    assert retention_bars("M1") == 1600 and retention_bars("M15") == 800
    assert 'gap_bars": gap_count(bars[-120:], payload.timeframe)' in open(
        os.path.join(ROOT, "routes", "bridge_routes.py"), encoding="utf-8").read()


def test_shadow_replay_prefers_ea_m1_feed():
    src = open(os.path.join(ROOT, "deferred_shadow.py"), encoding="utf-8").read()
    body = src.split("async def m1_bars_after(")[1].split("\nasync def ")[0]
    assert '"timeframe": "M1"' in body and body.index("intraday_candles") < body.index("scalp_ticks")


def test_release_runbook_and_install_guide_exist():
    doc = open(os.path.join(ROOT, "..", "docs", "EA_1_65_RELEASE.md"), encoding="utf-8").read()
    for needle in ("RoboForex Demo", "24 h", "ea-release", "candidate", "AutoTrading", "1.65", "rollback", "new_tp", "M1"):
        assert needle in doc, needle
    assert re.search(r"promote.*0 failures|0 failures.*promote", doc, re.I | re.S)
