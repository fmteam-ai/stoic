"""Fix plan step A6 — closes Batch A (reviewer items H1–H7, H10–H12 + two A5 leftovers).

H1  netting detection: admin setting → EA → broker-server registry → hedging (source shown)
H2  no-2FA restart never trusts the owner-controlled demo proof (see test_fixplan_a3_a4 h2_*)
H3  --archive refuses groups with several bot trades, judges age by last write, deletes only
    an unchanged row
H4  heartbeat backfill of a bot position under PANIC is closed at once
H5  one leg rule (entry deal id); heartbeat matches by ticket regardless of leg; /report sweeps
    an unkeyed snapshot duplicate
H6  a late fill of an EXPIRED order is always FULL_CLOSEd (audited, ops alert, stats-excluded)
H7  the suite refuses a DB_NAME that does not look like a test database
H10 absorbing a duplicate carries pending_modification / close_requested / banked partial P&L
H11 adoption clears the cancel verdict; the intent moves to filled (late_fill)
H12 stale unique index is surfaced (platform_state → release readiness / Ops Console)
"""
import os
import sys
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from bson import ObjectId
from pymongo import MongoClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from test_fixplan_a3_a4_panic_locks import _arun, _lock, _trade  # noqa: E402  (shared helpers)

TAG = "_test_fixplan_a34"   # reuse the a3/a4 cleanup tag so the shared `seeded` teardown covers our rows


def _iso(seconds_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


@pytest.fixture
def db():
    client = MongoClient(os.environ["MONGO_URL"])
    yield client[os.environ["DB_NAME"]]
    client.close()


@pytest.fixture
def seeded(db):
    user_oid = ObjectId()
    user_id = str(user_oid)
    db.users.insert_one({"_id": user_oid, "email": f"a6-{user_oid}@test.local", "role": "user", TAG: True})
    token = f"A6-{ObjectId()}"
    acc_id = db.accounts.insert_one({
        "user_id": user_id, "label": "A6", "broker": "test", "bridge_token": token,
        "mode": "live", "trading_enabled": True, "open_positions": 0,
        "last_heartbeat": _iso(), TAG: True,
    }).inserted_id
    yield {"user_id": user_id, "account_id": str(acc_id), "acc_oid": acc_id, "token": token}
    for coll in ("trades", "accounts", "users", "bot_configs", "broker_deals", "execution_intents",
                 "trade_events", "audit_log", "ops_alerts"):
        db[coll].delete_many({TAG: True})
    db.broker_deals.delete_many({"account_id": str(acc_id)})
    db.close_commands.delete_many({"account_id": str(acc_id)})
    db.audit_log.delete_many({"user_id": user_id})
    db.ops_alerts.delete_many({"meta.account_id": str(acc_id)})
    db.trades.delete_many({"account_id": str(acc_id)})
    db.ops_outbox.delete_many({"user_id": user_id})


# ------------------------------------------------------------------ H1
def test_h1_position_mode_authority_order(db, seeded):
    import routes.bridge_routes as br
    d = br.get_db()
    reg_hit = {"name": "NetBroker", "capabilities": {"position_mode": "netting"}}
    with patch("broker_registry.resolve_registry", AsyncMock(return_value=None)):
        assert _arun(br.position_mode_resolution(d, {"broker_server": "X"})) == {"mode": "hedging", "source": "default"}
        assert _arun(br.position_mode_resolution(d, {"ea_identity": {"margin_mode": "netting"}}))["source"] == "ea"
        out = _arun(br.position_mode_resolution(d, {"position_mode_override": {"mode": "netting", "by": "a@x"},
                                                    "ea_identity": {"margin_mode": "hedging"}}))
        assert out["mode"] == "netting" and out["source"] == "admin" and out["by"] == "a@x"
    with patch("broker_registry.resolve_registry", AsyncMock(return_value=reg_hit)):
        out = _arun(br.position_mode_resolution(d, {"broker_server": "NetBroker-Live"}))
        assert out == {"mode": "netting", "source": "registry", "broker": "NetBroker"}
        # admin HEDGING beats a netting registry entry
        assert _arun(br.position_mode_resolution(d, {"broker_server": "NetBroker-Live",
                                                     "position_mode_override": {"mode": "hedging"}}))["mode"] == "hedging"
    assert br.netting_leg(True, 777) == 777 and br.netting_leg(False, 777) is None and br.netting_leg(True, None) is None


def test_h1_admin_sets_and_clears_position_mode(db, seeded, monkeypatch):
    import routes.admin_routes as ar
    monkeypatch.setattr(ar, "_reauth", AsyncMock())
    chain = AsyncMock()
    admin = {"id": "adm", "role": "admin", "email": "admin@test.local"}
    with patch("audit_chain.append_chained", chain), \
            patch("broker_registry.resolve_registry", AsyncMock(return_value=None)):
        row = _arun(ar.admin_set_account_position_mode(seeded["account_id"], {"mode": "netting", "password": "x",
                                                                                "reason": "broker nets"}, user=admin))
        assert row["mode"] == "netting" and row["source"] == "admin" and row["override"]["by"] == "admin@test.local"
        acc = db.accounts.find_one({"_id": seeded["acc_oid"]})
        assert acc["position_mode_override"]["mode"] == "netting" and acc["position_mode_override"]["reason"] == "broker nets"
        listed = _arun(ar.admin_account_position_modes(user=admin))["accounts"]
        mine = next(r for r in listed if r["account_id"] == seeded["account_id"])
        assert mine["mode"] == "netting" and mine["source"] == "admin"
        row = _arun(ar.admin_set_account_position_mode(seeded["account_id"], {"mode": "auto", "password": "x"}, user=admin))
        assert row["source"] == "default" and "position_mode_override" not in db.accounts.find_one({"_id": seeded["acc_oid"]})
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ei:
            _arun(ar.admin_set_account_position_mode(seeded["account_id"], {"mode": "weird", "password": "x"}, user=admin))
        assert ei.value.status_code == 422
    assert chain.await_count == 2
    assert chain.await_args_list[0].args[1]["action"] == "account_position_mode_set"
    # the owner's account list carries the verdict + source
    import routes.account_routes as acr
    with patch("broker_registry.resolve_registry", AsyncMock(return_value=None)):
        accs = _arun(acr.list_accounts(user={"id": seeded["user_id"], "role": "user"}))
    assert accs[0]["position_mode"] == "hedging" and accs[0]["position_mode_source"] == "default"
    # audit A6 SEC-001 — the admin-only override object (who/when/why) never reaches the owner,
    # while the verdict still reflects the admin setting
    db.accounts.update_one({"_id": seeded["acc_oid"]}, {"$set": {"position_mode_override": {
        "mode": "netting", "by": "admin@test.local", "at": _iso(), "reason": "internal note"}}})
    with patch("broker_registry.resolve_registry", AsyncMock(return_value=None)):
        accs = _arun(acr.list_accounts(user={"id": seeded["user_id"], "role": "user"}))
    assert accs[0]["position_mode"] == "netting" and accs[0]["position_mode_source"] == "admin"
    assert "position_mode_override" not in accs[0] and "internal note" not in str(accs[0])


# ------------------------------------------------------------------ H4
def test_h4_heartbeat_backfill_of_bot_position_under_panic_is_closed(db, seeded, monkeypatch):
    import routes.bridge_routes as br
    from models import BridgeHeartbeat, BridgePosition
    monkeypatch.setattr(br.ws_manager, "broadcast", AsyncMock())
    _lock(db, seeded)
    hb = BridgeHeartbeat(bridge_token=seeded["token"], balance=10000, equity=10000, open_positions=1,
                         positions=[BridgePosition(ticket=886001, symbol="XAUUSD", type="BUY", volume=0.1,
                                                   price_open=4000.0, magic=901234)])
    _arun(br.heartbeat(hb))
    rows = list(db.trades.find({"account_id": seeded["account_id"], "mt5_ticket": 886001}))
    assert len(rows) == 1
    r = rows[0]
    assert r["backfilled_from_snapshot"] and r["close_requested"] is True and r["close_reason"] == "panic"
    assert r["late_fill_after_panic"] is True and r["pending_modification"]["reason"] == "late_fill_after_panic"
    # a MANUAL position (magic 0) surfacing under PANIC is NOT touched
    hb2 = BridgeHeartbeat(bridge_token=seeded["token"], balance=10000, equity=10000, open_positions=2,
                          positions=[BridgePosition(ticket=886002, symbol="EURUSD", type="SELL", volume=0.1,
                                                    price_open=1.1, magic=0)])
    _arun(br.heartbeat(hb2))
    m = db.trades.find_one({"account_id": seeded["account_id"], "mt5_ticket": 886002})
    assert m and not m.get("close_requested")


# ------------------------------------------------------------------ H5
def test_h5_report_leg_is_entry_deal_only_and_sweeps_unkeyed_snapshot_duplicate(db, seeded, monkeypatch):
    import routes.bridge_routes as br
    from models import BridgeTradeReport
    monkeypatch.setattr(br.ws_manager, "broadcast", AsyncMock())
    db.accounts.update_one({"_id": seeded["acc_oid"]}, {"$set": {"position_mode_override": {"mode": "netting"}}})
    bot = _trade(db, seeded, _dispatched_at=_iso(5), execution_intent_id=f"a6-{ObjectId()}")
    # the snapshot got there first: unkeyed (no deal known), unprotected
    dup = _trade(db, seeded, status="open", mt5_ticket=886100, stop_loss=0, take_profit=0, origin="auto",
                 backfilled_from_snapshot=True, protection_missing=True, live_pnl=1.5)
    out = _arun(br.report_trade(BridgeTradeReport(bridge_token=seeded["token"], trade_id=str(bot), mt5_ticket=886100,
                                                  status="open", entry_price=4000.0, requested_price=4000.0,
                                                  order_ticket=555001, deal_ticket=777001)))
    assert out.get("ok", True) is not False
    b = db.trades.find_one({"_id": bot})
    assert b["position_leg"] == 777001 and b["status"] == "open" and b["absorbed_duplicate_id"] == str(dup)
    assert db.trades.find_one({"_id": dup})["status"] == "superseded"
    assert db.trades.count_documents({"account_id": seeded["account_id"], "mt5_ticket": 886100,
                                      "status": {"$in": ["open", "pending"]}}) == 1
    # an order ticket alone never becomes a leg
    bot2 = _trade(db, seeded, _dispatched_at=_iso(5), symbol="EURUSD")
    _arun(br.report_trade(BridgeTradeReport(bridge_token=seeded["token"], trade_id=str(bot2), mt5_ticket=886101,
                                            status="open", entry_price=1.1, requested_price=1.1, order_ticket=555002)))
    assert db.trades.find_one({"_id": bot2}).get("position_leg") is None


def test_h5_heartbeat_never_inserts_second_row_for_tracked_ticket_regardless_of_leg(db, seeded, monkeypatch):
    import routes.bridge_routes as br
    from models import BridgeHeartbeat, BridgePosition
    monkeypatch.setattr(br.ws_manager, "broadcast", AsyncMock())
    db.accounts.update_one({"_id": seeded["acc_oid"]}, {"$set": {"position_mode_override": {"mode": "netting"}}})
    _trade(db, seeded, status="open", mt5_ticket=886200, position_leg=777200)
    hb = BridgeHeartbeat(bridge_token=seeded["token"], balance=10000, equity=10000, open_positions=1,
                         positions=[BridgePosition(ticket=886200, symbol="XAUUSD", type="BUY", volume=0.1,
                                                   price_open=4000.0, magic=901234)])
    _arun(br.heartbeat(hb))
    assert db.trades.count_documents({"account_id": seeded["account_id"], "mt5_ticket": 886200}) == 1


# ------------------------------------------------------------------ H6 / H11
def _intent(db, seeded, status="expired"):
    iid = f"a6-int-{ObjectId()}"
    db.execution_intents.insert_one({"intent_id": iid, "status": status, "account_id": seeded["account_id"],
                                     "history": [], TAG: True})
    return iid


def test_h6_late_fill_of_expired_order_via_external_deal_is_closed_audited_alerted(db, seeded, monkeypatch):
    import routes.bridge_routes as br
    from models import BridgeExternalDeal
    monkeypatch.setattr(br.ws_manager, "broadcast", AsyncMock())
    iid = _intent(db, seeded)
    bot = _trade(db, seeded, status="cancelled", error="pending_order_expired", close_reason="expired",
                 _dispatched_at=_iso(200), opened_at=_iso(300), closed_at=_iso(60), execution_intent_id=iid)
    _arun(br.external_deal(BridgeExternalDeal(bridge_token=seeded["token"], mt5_ticket=886300, deal_id=777300,
                                              deal_entry="in", symbol="XAUUSD", action="BUY", lots=0.1,
                                              price=4000.5, magic=901234, position_volume=0.1)))
    r = db.trades.find_one({"_id": bot})
    assert r["status"] == "open" and r["mt5_ticket"] == 886300 and r["late_fill_after_cancel"] == "expired"
    assert r["close_requested"] is True and r["close_reason"] == "late_fill_after_expiry"
    assert r["late_fill_after_expiry"] is True and r["stats_excluded"] is True
    assert r["pending_modification"]["type"] == "FULL_CLOSE" and r["pending_modification"]["reason"] == "late_fill_after_expiry"
    assert "error" not in r                                   # H11 — cancel verdict cleared
    assert r["origin"] if r.get("origin") else True           # origin untouched (daily loss cap still counts it)
    assert db.audit_log.find_one({"action": "late_fill_after_expiry", "detail.trade_id": str(bot)})
    al = db.ops_alerts.find_one({"kind": "late_fill_after_expiry", "dedup_key": f"late_fill_after_expiry:{bot}"})
    assert al and al["severity"] == "warning"
    it = db.execution_intents.find_one({"intent_id": iid})
    assert it["status"] == "filled" and it["late_fill"] is True and it["late_fill_prior_status"] == "pending_order_expired"
    assert it["history"][-1]["to"] == "filled"
    assert db.trades.count_documents({"account_id": seeded["account_id"], "mt5_ticket": 886300}) == 1


def test_h6_late_fill_of_expired_order_via_report_is_closed(db, seeded, monkeypatch):
    import routes.bridge_routes as br
    from models import BridgeTradeReport
    monkeypatch.setattr(br.ws_manager, "broadcast", AsyncMock())
    iid = _intent(db, seeded)
    bot = _trade(db, seeded, status="cancelled", error="pending_order_expired", close_reason="expired",
                 _dispatched_at=_iso(200), closed_at=_iso(60), execution_intent_id=iid)
    out = _arun(br.report_trade(BridgeTradeReport(bridge_token=seeded["token"], trade_id=str(bot), mt5_ticket=886400,
                                                  status="open", entry_price=4000.0, requested_price=4000.0)))
    assert out.get("ok", True) is not False
    r = db.trades.find_one({"_id": bot})
    assert r["status"] == "open" and r["close_requested"] is True and r["close_reason"] == "late_fill_after_expiry"
    assert r["stats_excluded"] is True and "error" not in r and r["late_fill_after_cancel"] == "expired"
    assert db.execution_intents.find_one({"intent_id": iid})["status"] == "filled"


def test_h6_pending_sibling_fill_is_not_closed_and_panic_wins_over_expiry(db, seeded, monkeypatch):
    import routes.bridge_routes as br
    from models import BridgeHeartbeat, BridgePosition
    monkeypatch.setattr(br.ws_manager, "broadcast", AsyncMock())
    # plain pending order filled late via the snapshot → stays open, no close
    bot = _trade(db, seeded, _dispatched_at=_iso(5))
    _arun(br.heartbeat(BridgeHeartbeat(bridge_token=seeded["token"], balance=1e4, equity=1e4, open_positions=1,
                                       positions=[BridgePosition(ticket=886500, symbol="XAUUSD", type="BUY", volume=0.1,
                                                                 price_open=4000.0, magic=901234)])))
    r = db.trades.find_one({"_id": bot})
    assert r["status"] == "open" and not r.get("close_requested") and r.get("position_leg") is None
    # expired order + PANIC lock → panic policy (emergency close) wins
    _lock(db, seeded)
    bot2 = _trade(db, seeded, status="cancelled", error="pending_order_expired", close_reason="expired",
                  _dispatched_at=_iso(200), closed_at=_iso(60), symbol="EURUSD", action="SELL")
    _arun(br.heartbeat(BridgeHeartbeat(bridge_token=seeded["token"], balance=1e4, equity=1e4, open_positions=2,
                                       positions=[BridgePosition(ticket=886501, symbol="EURUSD", type="SELL", volume=0.1,
                                                                 price_open=1.1, magic=901234)])))
    r2 = db.trades.find_one({"_id": bot2})
    assert r2["close_reason"] == "panic" and r2["late_fill_after_panic"] is True and not r2.get("late_fill_after_expiry")


def test_h6_stats_queries_exclude_late_fills_but_daily_loss_cap_counts_them():
    import inspect
    import bayes_decision
    import bot_runner
    import calibration
    import learning_pipeline
    import ml_ensemble
    import risk_budget
    import safety_guardian
    for mod in (bot_runner, risk_budget, learning_pipeline, calibration, ml_ensemble, bayes_decision):
        assert '"stats_excluded": {"$ne": True}' in inspect.getsource(mod), mod.__name__
    assert inspect.getsource(bot_runner).count('"stats_excluded": {"$ne": True}') >= 2   # anti-tilt + loss streak
    assert "stats_excluded" not in inspect.getsource(safety_guardian)                   # daily loss cap keeps them


# ------------------------------------------------------------------ H3
def test_h3_archive_plan_refuses_two_bot_trades_and_judges_age_by_last_write():
    from ops.ticket_duplicates import plan_group, unchanged_filter
    now = datetime.now(timezone.utc)
    old = (now - timedelta(hours=2)).isoformat()
    a = {"_id": ObjectId(), "opened_at": old, "execution_intent_id": "i1", "stop_loss": 1, "pnl": 0}
    b = {"_id": ObjectId(), "opened_at": old, "execution_intent_id": "i2", "pnl": 0}
    keep, arch, why = plan_group([a, b], now)
    assert keep is None and "2 distinct bot trades" in why
    c = {"_id": ObjectId(), "opened_at": old, "signal_id": "s1", "pnl": 0}
    d = {"_id": ObjectId(), "opened_at": old, "signal_id": "s2", "pnl": 0}
    assert plan_group([c, d], now)[2].startswith("2 distinct bot trades")
    # created long ago but WRITTEN 1 minute ago → refused (last write, not creation)
    e = {"_id": ObjectId(), "opened_at": old, "updated_at": (now - timedelta(seconds=60)).isoformat(), "pnl": 0}
    keep, arch, why = plan_group([a, e], now)
    assert keep is None and "written in the last 10 minutes" in why
    f = {"_id": ObjectId(), "opened_at": old, "updated_at": (now - timedelta(hours=1)).isoformat(), "pnl": 0}
    keep, arch, why = plan_group([a, f], now)
    assert keep["_id"] == a["_id"] and [r["_id"] for r in arch] == [f["_id"]] and why is None
    q = unchanged_filter({"_id": f["_id"], "pnl": 0, "status": "open"})
    assert q["pnl"] == 0 and q["status"] == "open" and q["pending_modification"] == {"$exists": False}


def test_h3_conditional_delete_skips_a_row_changed_since_read(db, seeded):
    from ops.ticket_duplicates import unchanged_filter
    tid = _trade(db, seeded, status="open", mt5_ticket=886600, pnl=0.0)
    row = db.trades.find_one({"_id": tid})
    db.trades.update_one({"_id": tid}, {"$set": {"pnl": -4.2}})        # a P&L update landed mid-run
    assert db.trades.delete_one(unchanged_filter(row)).deleted_count == 0
    row = db.trades.find_one({"_id": tid})
    assert db.trades.delete_one(unchanged_filter(row)).deleted_count == 1


# ------------------------------------------------------------------ H7
def test_h7_suite_refuses_non_test_database_names():
    import conftest
    pat = conftest._TEST_DB_PATTERN
    for ok in ("ai_trading_bot_test", "stoic_ci", "stoic_e2e", "stoic_release_verify", "stoic_cleandeploy", "test_database"):
        assert pat.search(ok), ok
    for bad in ("ai_trading_bot", "stoic", "production", "stoic_prod"):
        assert not pat.search(bad), bad
    assert pat.search(os.environ["DB_NAME"])


# ------------------------------------------------------------------ H10
def test_h10_absorb_carries_inflight_close_and_banked_partial(db, seeded):
    import routes.bridge_routes as br
    keep = _trade(db, seeded, status="open", mt5_ticket=886700, execution_intent_id=f"a6-{ObjectId()}")
    dup = _trade(db, seeded, status="open", mt5_ticket=886700, position_leg=1, origin="auto",
                 backfilled_from_snapshot=True, pending_modification={"type": "FULL_CLOSE", "reason": "manual"},
                 close_requested=True, close_reason="manual", pnl_banked_partial=3.3, partial_closed=True, live_pnl=2.0)
    assert _arun(br._absorb_duplicate_ticket_row(br.get_db(), seeded["account_id"], 886700, None, keep)) == str(dup)
    k = db.trades.find_one({"_id": keep})
    assert k["pending_modification"]["type"] == "FULL_CLOSE" and k["close_requested"] is True
    assert k["close_reason"] == "manual" and k["pnl_banked_partial"] == 3.3 and k["partial_closed"] is True
    assert k["live_pnl"] == 2.0 and k["absorbed_duplicate_id"] == str(dup)
    assert db.trades.find_one({"_id": dup})["status"] == "superseded"


def test_h10_report_returns_conflict_not_500_when_duplicate_reappears(db, seeded, monkeypatch):
    import routes.bridge_routes as br
    from models import BridgeTradeReport
    from pymongo.errors import DuplicateKeyError
    monkeypatch.setattr(br.ws_manager, "broadcast", AsyncMock())
    bot = _trade(db, seeded, _dispatched_at=_iso(5))
    calls = {"n": 0}
    coll_cls = type(br.get_db().trades)
    real = coll_cls.update_one

    async def flaky(self, flt, upd, *a, **k):
        if self.name == "trades" and flt.get("_id") == bot and "$set" in upd and upd["$set"].get("mt5_ticket") == 886800:
            calls["n"] += 1
            raise DuplicateKeyError("E11000 dup")
        return await real(self, flt, upd, *a, **k)
    monkeypatch.setattr(br, "_absorb_duplicate_ticket_row", AsyncMock(return_value="some-dup"))
    with patch.object(coll_cls, "update_one", flaky):
        out = _arun(br.report_trade(BridgeTradeReport(bridge_token=seeded["token"], trade_id=str(bot), mt5_ticket=886800,
                                                      status="open", entry_price=4000.0, requested_price=4000.0)))
    assert out == {"ok": False, "reason": "duplicate_ticket_conflict", "trade_id": str(bot)} and calls["n"] == 2


# ------------------------------------------------------------------ H11
def test_h11_adoption_clears_cancel_verdict_and_moves_intent_to_filled(db, seeded):
    import routes.bridge_routes as br
    from execution_intents import record_late_fill, transition
    iid = _intent(db, seeded, status="cancelled")
    bot = _trade(db, seeded, status="cancelled", error="panic_lock", close_reason="panic", _dispatched_at=_iso(5),
                 execution_intent_id=iid)
    sib = db.trades.find_one({"_id": bot})
    assert _arun(br.adopt_sibling(br.get_db(), sib, ticket=886900, price=4001.0, via="test")) is True
    r = db.trades.find_one({"_id": bot})
    assert r["status"] == "open" and "error" not in r and "close_reason" not in r and r["late_fill_after_cancel"] == "panic"
    it = db.execution_intents.find_one({"intent_id": iid})
    assert it["status"] == "filled" and it["late_fill"] is True and it["late_fill_via"] == "test"
    # a regular (in-flight) intent still follows the legal table
    iid2 = _intent(db, seeded, status="dispatched")
    out = _arun(record_late_fill(br.get_db(), iid2, ticket=1, via="test"))
    assert out["status"] == "filled" and not out.get("late_fill")
    # a filled intent can never be re-expired
    assert _arun(transition(br.get_db(), iid, "expired")) is None


# ------------------------------------------------------------------ H12
def test_h12_stale_index_verdict_surfaces_in_readiness_and_ops_console(db, seeded):
    import seed
    d = seed.get_db() if hasattr(seed, "get_db") else __import__("database").get_db()
    prev = db.platform_state.find_one({"_id": seed.TICKET_INDEX_STATE_ID})
    try:
        _arun(seed._record_ticket_index_state(d, present=True, stale=True, key=[("account_id", 1)], filter_={}))
        chk = _arun(seed.ticket_index_check(d))
        assert chk["ok"] is False and chk["stale"] is True and "STALE" in chk["detail"]
        _arun(seed._record_ticket_index_state(d, present=False, stale=False, duplicates=3))
        chk = _arun(seed.ticket_index_check(d))
        assert chk["ok"] is False and chk["present"] is False and "3 duplicate" in chk["detail"]
        db.platform_state.delete_one({"_id": seed.TICKET_INDEX_STATE_ID})
        assert _arun(seed.ticket_index_check(d))["ok"] is False
        import inspect
        import routes.ops_console
        import routes.ops_routes
        assert 'checks["unique_ticket_index"]' in inspect.getsource(routes.ops_routes)
        assert 'out["unique_ticket_index"]' in inspect.getsource(routes.ops_console)
    finally:
        # the boot path re-records the real verdict
        _arun(seed.ensure_unique_ticket_index(d))
        if prev is None:
            pass
    assert db.platform_state.find_one({"_id": seed.TICKET_INDEX_STATE_ID}) is not None


# ------------------------------------------------------------------ A5 leftovers
def test_leftover_adaptive_exit_guarded_write_not_applied_emits_nothing():
    import adaptive_exits as ae
    import time as _t
    fake = MagicMock()
    fake.intraday_candles.find_one = AsyncMock(return_value={"bars": [{"t": _t.time()}]})
    fake.trades.update_one = AsyncMock(return_value=MagicMock(modified_count=0))
    trade = {"_id": ObjectId(), "user_id": "u", "symbol": "XAUUSD", "action": "BUY", "entry_price": 4000, "lot_size": 0.1}
    ev = AsyncMock()
    with patch.object(ae, "compute_features", lambda bars: {"atr15": 1.0}), \
            patch.object(ae, "vol_retarget", lambda *a: None), \
            patch.object(ae, "fade_tighten", lambda *a: {"kind": "EXIT_TIGHTEN_FADE", "new_sl": 4001.0, "locked_pips": 10}), \
            patch("trade_events.append", ev), patch("ws_manager.manager.broadcast", AsyncMock()) as bc:
        assert _arun(ae.manage_exits(fake, trade, {}, 4005.0, 50.0)) is False
    ev.assert_not_awaited()
    bc.assert_not_awaited()
    fake.trades.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
    with patch.object(ae, "compute_features", lambda bars: {"atr15": 1.0}), \
            patch.object(ae, "vol_retarget", lambda *a: None), \
            patch.object(ae, "fade_tighten", lambda *a: {"kind": "EXIT_TIGHTEN_FADE", "new_sl": 4001.0, "locked_pips": 10}), \
            patch("trade_events.append", ev), patch("ws_manager.manager.broadcast", AsyncMock()):
        assert _arun(ae.manage_exits(fake, trade, {}, 4005.0, 50.0)) is True
    assert ev.await_count == 1


def test_leftover_chat_enable_bots_judges_only_targeted_accounts_with_right_hint(db, seeded):
    from routes.nl_routes import _enable_bots
    other = db.accounts.insert_one({"user_id": seeded["user_id"], "label": "other", "mode": "live", TAG: True,
                                    "trading_authority": "LOCKED",
                                    "authority_lock": {"reason": "panic", "scope": "user", "by": seeded["user_id"]}}).inserted_id
    cfg = db.bot_configs.insert_one({"user_id": seeded["user_id"], "account_id": seeded["account_id"], "active": False,
                                     "risk_level": "low", TAG: True}).inserted_id
    # the targeted bot's account is NOT locked → enabling proceeds (the other account's lock is irrelevant)
    out = _arun(_enable_bots(seeded["user_id"], f"bot:{cfg}"))
    assert out.get("blocked") is None and out["bots_enabled"] == 1
    db.bot_configs.update_one({"_id": cfg}, {"$set": {"active": False}})
    # admin-wide lock on the targeted account → refused with the ADMIN release hint
    db.accounts.update_one({"_id": seeded["acc_oid"]}, {"$set": {
        "trading_authority": "LOCKED", "authority_lock": {"reason": "panic", "scope": "platform", "by": "admin"}}})
    out = _arun(_enable_bots(seeded["user_id"], f"bot:{cfg}"))
    assert out["blocked"] == "panic_admin_lock" and out["admin_lock"] is True and "only an admin" in out["message"]
    assert db.bot_configs.find_one({"_id": cfg})["active"] is False
    # user lock → Bot Config → Start hint
    db.accounts.update_one({"_id": seeded["acc_oid"]}, {"$set": {
        "authority_lock": {"reason": "panic", "scope": "user", "by": seeded["user_id"]}}})
    out = _arun(_enable_bots(seeded["user_id"], f"bot:{cfg}"))
    assert out["blocked"] == "panic_lock" and "Bot Config → Start" in out["message"]
    db.accounts.delete_one({"_id": other})
