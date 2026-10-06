"""main96 review — step A12: Q-1 netting closes on every path, Q-2 installation authority
hand-over, Q-3 rollback order, D-1/D-2 readiness, Bot Config default cap, late-fill alert
dedupe. Run with DB_NAME="" (pure unit)."""
import asyncio
import inspect
import os
import sys
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


# ── Q-1: two netted trades on one broker position ────────────────────────────
def test_two_netted_trades_close_one_leg_per_poll_with_exact_volumes():
    from routes.bridge_routes import plan_netting_close_legs
    t1 = {"_id": ObjectId(), "symbol": "XAUUSD", "mt5_ticket": 7, "lot_size": 0.10, "live_volume": 0.30, "close_idem_key": "k1", "close_seq": 1}
    t2 = {"_id": ObjectId(), "symbol": "XAUUSD", "mt5_ticket": 7, "lot_size": 0.20, "live_volume": 0.30, "close_idem_key": "k2", "close_seq": 1}
    # poll 1 — only the first leg goes out: leave 0.30 − 0.10 = 0.20 lots on the broker
    passthrough, legs, seen = plan_netting_close_legs([t1, t2], netting=True)
    assert passthrough == [] and seen == {7} and len(legs) == 1
    assert legs[0]["trade_id"] == str(t1["_id"]) and legs[0]["type"] == "PARTIAL_CLOSE"
    assert legs[0]["new_volume"] == 0.2 and legs[0]["close_volume"] == 0.1 and legs[0]["intent_id"] == "k1"
    # poll 2 — t1 is gone, the heartbeat refreshed live_volume to 0.20: t2 now owns the whole
    # position → plain by-ticket close (no over-close, no leftover)
    t2["live_volume"] = 0.20
    passthrough, legs, _ = plan_netting_close_legs([t2], netting=True)
    assert passthrough == [t2] and legs == []
    # hedging account: untouched
    passthrough, legs, _ = plan_netting_close_legs([t1, t2], netting=False)
    assert passthrough == [t1, t2] and legs == []


def test_close_path_ack_maps_to_full_close_and_never_redispatches():
    from routes import bridge_routes
    src = inspect.getsource(bridge_routes.modification_ack)
    assert 'payload.intent_id == trade.get("close_idem_key")' in src and 'update["netting_close_acked"] = True' in src
    poll = inspect.getsource(bridge_routes.poll_trades)
    assert '"netting_close_acked": {"$ne": True}' in poll and "plan_netting_close_legs(close_rows, netting_acc)" in poll
    assert '"live_volume": 1' in poll                     # the close cursor carries the broker volume


# ── Q-2: running installation stays authoritative until the new one heartbeats ─
def test_pairing_keeps_running_installation_until_new_token_first_use():
    import bridge_tokens as bt
    from routes import setup_routes
    src = inspect.getsource(setup_routes)
    assert '"pending_first_heartbeat": True' in src and "if not _has_live:" in src
    assert '"revoked": {"$ne": True}, "pending_first_heartbeat": True}' in src      # only stale pendings are revoked at claim
    db = FakeDb()
    acc = {"_id": ObjectId(), "user_id": "u1", "bridge_token_hash": "new", "bridge_token_prev_hash": "old"}
    db.accounts.rows.append(acc)
    aid = str(acc["_id"])
    db.installations.rows += [{"_id": ObjectId(), "installation_id": "OLD", "account_id": aid, "revoked": False},
                              {"_id": ObjectId(), "installation_id": "NEW", "account_id": aid, "revoked": False, "pending_first_heartbeat": True}]
    db.execution_leases.rows.append({"account_id": aid, "installation_id": "OLD", "revoked": False})
    with patch.dict(os.environ, {"BRIDGE_TOKEN_HASH_KEY": "unit-test-not-a-secret"}):
        run(bt.retire_prev(db, acc))
    by = {i["installation_id"]: i for i in db.installations.rows}
    assert by["OLD"]["revoked"] is True and by["NEW"].get("revoked") is False and "pending_first_heartbeat" not in by["NEW"]
    assert db.execution_leases.rows[0]["installation_id"] == "NEW"
    assert "bridge_token_prev_hash" not in db.accounts.rows[0] and "old" in db.accounts.rows[0]["bridge_token_retired_hashes"]
    # no pending registration → no-op
    assert run(bt.promote_pending_installation(db, acc)) is None


# ── Q-3: stop → restore data → old code → start ──────────────────────────────
def test_rollback_order_data_before_code_and_restore_no_start():
    rb = _read("deploy", "rollback.sh")
    assert rb.index('RESTORE_NO_START=1 deploy/backup.sh restore "${WITH_DB}"') < rb.index('git checkout --detach "${REF}"') < rb.index("compose_up")
    up = _read("deploy", "update.sh")
    body = up[up.index("rollback() {"):]
    body = body[:body.index("\n}\n")]
    assert body.index('RESTORE_NO_START=1 deploy/backup.sh restore "${PRE_BACKUP}"') < body.index('git checkout --detach "${PREV}"') < body.index("compose_up")
    bk = _read("deploy", "backup.sh")
    assert 'if [ "${RESTORE_NO_START:-0}" = "1" ]' in bk and bk.index("RESTORE_NO_START") < bk.index("docker compose start backend")


# ── D-1 / D-2 readiness ───────────────────────────────────────────────────────
def test_readiness_accepts_157_on_demo_and_warns_on_netting():
    import demo_readiness as dr
    rows = [{"label": "Demo-3", "heartbeat_fresh": True, "ea_current": True, "attested_demo": True,
             "position_mode_explicit": True, "caps_explicit": True, "netting": True}]
    c = {x["id"]: x for x in dr.fleet_checks(rows)}
    assert c["fleet_netting"]["status"] == "warn" and "Demo-3" in c["fleet_netting"]["detail"]
    rows[0]["netting"] = False
    assert {x["id"]: x for x in dr.fleet_checks(rows)}["fleet_netting"]["status"] == "pass"
    assert "1.57" in dr.DEMO_ACCEPTED_EA
    src = inspect.getsource(dr.fleet)
    assert "ea_v == LATEST_EA or (demo and ea_v in DEMO_ACCEPTED_EA)" in src


# ── A12-5: Bot Config default cap + one late-fill alert per trade ─────────────
def test_bot_config_shows_server_default_cap_and_late_fill_alert_once_per_trade():
    bc = _read("frontend", "src", "pages", "BotConfig.jsx")
    assert 'field="trade_of_day_cap"' in bc and "fallback={1}" in bc and "cfg[field] ?? fallback" in bc
    import execution_health as eh
    db = FakeDb()
    acc = {"_id": ObjectId(), "user_id": None, "label": "Demo"}
    db.accounts.rows.append(acc)
    aid = str(acc["_id"])
    calls = []

    async def _spy(_db, _acc, account_id, count, detail):
        calls.append((account_id, count))

    with patch.object(eh, "_alert_late_fill", _spy):
        run(eh.record_event(db, aid, None, "late_fill", trade_id="t1", detail="first"))
        run(eh.record_event(db, aid, None, "late_fill", trade_id="t1", detail="re-reported"))
        run(eh.record_event(db, aid, None, "late_fill", trade_id="t2", detail="second trade"))
    assert [c[1] for c in calls] == [1, 2]            # t1 alerted once; t2 alerts with the distinct count 2
    assert len(db.execution_health_events.rows) == 3   # every report is still recorded
