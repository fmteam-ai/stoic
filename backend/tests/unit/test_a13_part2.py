"""A13 audit fix list — Part 2 (before live / real money). Run with DB_NAME="" (pure unit)."""
import asyncio
import inspect
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId
from pymongo.errors import DuplicateKeyError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_mongo import FakeCollection, FakeDb  # noqa: E402

pytestmark = pytest.mark.unit


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _UniqueDedupe(FakeCollection):
    async def insert_one(self, doc):
        if doc.get("dedupe_key") and any(r.get("dedupe_key") == doc["dedupe_key"] for r in self.rows):
            raise DuplicateKeyError("dup")
        return await super().insert_one(doc)


class _UniqueClientId(FakeCollection):
    async def insert_one(self, doc):
        if doc.get("client_order_id") and any(r.get("client_order_id") == doc["client_order_id"] for r in self.rows):
            raise DuplicateKeyError("dup")
        return await super().insert_one(doc)


def _db():
    db = FakeDb()
    db.execution_intents = _UniqueDedupe()
    db.trades = _UniqueClientId()
    return db


def _fake_client(fetch=None, create_raises=None, oco_raises=None):
    c = MagicMock()
    c.__aenter__ = AsyncMock(return_value=c)
    c.__aexit__ = AsyncMock(return_value=False)
    c.fetch_order_by_client_id = AsyncMock(return_value=fetch)
    c.create_market_order = AsyncMock(side_effect=create_raises) if create_raises else \
        AsyncMock(return_value={"id": "EX1", "status": "closed", "average": 60010.0})
    c.place_oco_protection = AsyncMock(side_effect=oco_raises) if oco_raises else \
        AsyncMock(return_value={"list_id": "L1", "list_client_order_id": "x-oco"})
    c.fetch_balance = AsyncMock(return_value={"total": {"USDT": 1000}})
    c.amount_to_precision = lambda sym, amt: float(amt)
    c.fetch_oco_status = AsyncMock(return_value=None)
    return c


# ── P0-01 crypto execution truth ─────────────────────────────────────────────
def test_intent_and_client_order_id_exist_before_any_exchange_call_and_dedupe():
    from crypto_bridge import crypto_execution as cx
    db = _db()
    sig = {"symbol": "BTCUSD", "action": "BUY", "lot_size": 0.01, "signal_id": "sig1", "stop_loss": 59000, "take_profit": 62000}
    it = run(cx.begin(db, account_id="a1", user_id="u", signal=sig, order_type="market",
                      ccxt_symbol="BTC/USDT", side="buy", amount=0.01))
    assert it["status"] == "dispatched" and it["client_order_id"] == cx.client_order_id(it["intent_id"])
    assert len(it["client_order_id"]) <= 36 and it["client_order_id"].startswith("stoic-")
    stored = db.execution_intents.rows[0]
    assert stored["client_order_id"] == it["client_order_id"] and stored["payload"]["exchange_symbol"] == "BTC/USDT"
    dup = run(cx.begin(db, account_id="a1", user_id="u", signal=sig, order_type="market",
                       ccxt_symbol="BTC/USDT", side="buy", amount=0.01))
    assert dup["blocked"] == "duplicate_intent" and dup["in_flight"] is True and len(db.execution_intents.rows) == 1


def test_failure_classification_rejected_vs_unknown_never_resent():
    import ccxt
    from crypto_bridge import crypto_execution as cx
    db = _db()
    it = run(cx.begin(db, account_id="a1", user_id="u", signal={"symbol": "BTCUSD", "signal_id": "s2"},
                      order_type="market", ccxt_symbol="BTC/USDT", side="buy", amount=0.01))
    out = run(cx.record_failure(db, it["intent_id"], ccxt.InsufficientFunds("insufficient funds")))
    assert out["blocked"] == "exchange_error" and db.execution_intents.rows[0]["status"] == "rejected"
    it2 = run(cx.begin(db, account_id="a1", user_id="u", signal={"symbol": "BTCUSD", "signal_id": "s3"},
                       order_type="market", ccxt_symbol="BTC/USDT", side="buy", amount=0.01))
    out2 = run(cx.record_failure(db, it2["intent_id"], ccxt.RequestTimeout("timeout")))
    assert out2["blocked"] == "exchange_unknown" and db.execution_intents.rows[1]["status"] == "unknown"
    assert "NOT be resent" in out2["reason"]


def test_restart_recovery_finds_order_by_client_id_and_records_exactly_once():
    from crypto_bridge import crypto_execution as cx
    db = _db()
    acc_id = ObjectId()
    db.accounts.rows.append({"_id": acc_id, "kind": "binance", "exchange_id": "binance"})
    it = run(cx.begin(db, account_id=str(acc_id), user_id="u",
                      signal={"symbol": "BTCUSD", "action": "BUY", "lot_size": 0.01, "signal_id": "s4",
                              "stop_loss": 59000, "take_profit": 62000},
                      order_type="market", ccxt_symbol="BTC/USDT", side="buy", amount=0.01))
    # "kill the process after the order is sent": intent dispatched, NO trade row
    assert db.trades.rows == []
    client = _fake_client(fetch={"id": "EX9", "status": "closed", "average": 60100.0})
    r1 = run(cx.reconcile_intents(db, lambda acc: client))
    r2 = run(cx.reconcile_intents(db, lambda acc: client))
    assert r1["recorded"] == 1 and r1["reconciled"] == 1
    assert len(db.trades.rows) == 1 and db.trades.rows[0]["client_order_id"] == it["client_order_id"]
    assert db.trades.rows[0]["recovered_from_exchange"] is True and db.trades.rows[0]["status"] == "open"
    assert db.execution_intents.rows[0]["status"] == "filled" and r2["recorded"] == 0
    client.create_market_order.assert_not_awaited()                     # never resent


def test_unknown_intent_not_at_exchange_becomes_failed_confirmed_after_grace():
    from crypto_bridge import crypto_execution as cx
    db = _db()
    acc_id = ObjectId()
    db.accounts.rows.append({"_id": acc_id, "kind": "binance"})
    it = run(cx.begin(db, account_id=str(acc_id), user_id="u", signal={"symbol": "BTCUSD", "signal_id": "s5"},
                      order_type="market", ccxt_symbol="BTC/USDT", side="buy", amount=0.01))
    run(cx.record_failure(db, it["intent_id"], TimeoutError()))
    client = _fake_client(fetch=None)
    later = datetime.now(timezone.utc) + timedelta(seconds=cx.UNKNOWN_GRACE_SEC + 1)
    assert run(cx.reconcile_intents(db, lambda a: client, now=datetime.now(timezone.utc)))["pending"] == 1
    assert run(cx.reconcile_intents(db, lambda a: client, now=later))["failed_confirmed"] == 1
    assert db.execution_intents.rows[0]["status"] == "failed_confirmed" and db.trades.rows == []


def test_protection_placed_or_fail_closed_flatten_and_authority_domain():
    from crypto_bridge import crypto_execution as cx
    import trading_authority as ta
    db = _db()
    tid = ObjectId()
    db.trades.rows.append({"_id": tid, "status": "open", "broker_kind": "binance", "account_id": "a1",
                           "protection": {"status": cx.PROTECTION_MISSING}})
    ok_client = _fake_client()
    prot = run(cx.protect(db, ok_client, trade_id=tid, ccxt_symbol="BTC/USDT", side="buy", amount=0.01,
                          stop_loss=59000, take_profit=62000, cid="stoic-x", account={}))
    assert prot["status"] == cx.PROTECTION_PLACED and db.trades.rows[0]["protection"]["list_id"] == "L1"
    assert run(ta.crypto_protection_domain(db, {"_id": "a1"}))["level"] == "FULL"
    # OCO fails → position flattened (fail closed) + trade closed
    db.trades.rows[0]["protection"] = {"status": cx.PROTECTION_MISSING}
    db.trades.rows[0]["status"] = "open"
    bad = _fake_client(oco_raises=RuntimeError("oco unsupported"))
    prot2 = run(cx.protect(db, bad, trade_id=tid, ccxt_symbol="BTC/USDT", side="buy", amount=0.01,
                           stop_loss=59000, take_profit=62000, cid="stoic-y", account={}))
    assert prot2["status"] == cx.PROTECTION_FLATTENED and db.trades.rows[0]["status"] == "closed"
    bad.create_market_order.assert_awaited_once()
    flat_kwargs = bad.create_market_order.await_args
    assert flat_kwargs.args[1] == "sell" and flat_kwargs.kwargs["client_order_id"] == "stoic-y-fl"   # N97-7 ≤ 36 chars
    # flatten ALSO fails → MISSING → authority CLOSE_ONLY for the account
    db.trades.rows[0]["status"] = "open"
    worse = _fake_client(oco_raises=RuntimeError("oco"), create_raises=RuntimeError("down"))
    prot3 = run(cx.protect(db, worse, trade_id=tid, ccxt_symbol="BTC/USDT", side="buy", amount=0.01,
                           stop_loss=59000, take_profit=62000, cid="stoic-z", account={}))
    assert prot3["status"] == cx.PROTECTION_MISSING
    assert run(ta.crypto_protection_domain(db, {"_id": "a1"}))["level"] == "CLOSE_ONLY"
    assert "crypto_protection" in ta._DOMAINS and ta.DOMAIN_SCOPE["crypto_protection"] == "account_bound"


def test_engine_order_path_is_intent_first_with_client_id_and_protection():
    from crypto_bridge.binance_engine import BinanceCCXTEngine
    src = inspect.getsource(BinanceCCXTEngine._engine_stage)
    assert src.index("cx.begin(") < src.index("create_market_order(") < src.index("cx.protect(")
    assert "client_order_id=cid" in src and "cx.record_failure" in src and "cx.record_outcome" in src
    assert "count_documents" not in src                               # caps come from the reservation
    from crypto_bridge.ccxt_engine import CCXTClient
    assert "clientOrderId" in inspect.getsource(CCXTClient.create_market_order)
    assert "privatePostOrderlistOco" in inspect.getsource(CCXTClient.place_oco_protection)   # N98-10 current endpoint
    import background_loops
    assert "reconcile_all" in inspect.getsource(background_loops._scalp_reconcile_loop)


# ── P1-02 account-wide reservation service ───────────────────────────────────
def test_reservation_caps_cover_every_entry_path_and_release_on_refusal():
    import account_reservations as ar
    db = FakeDb()
    acc = {"_id": ObjectId(), "trading_enabled": True}
    aid = str(acc["_id"])
    db.trades.rows.append({"user_id": "u", "account_id": aid, "status": "open", "origin": "auto",
                           "opened_at": datetime.now(timezone.utc).isoformat()})
    caps = {"auto_cap": 2, "total_cap": 4, "daily_cap": 10}
    r1 = run(ar.reserve_entry(db, account_id=aid, user_id="u", source="mt5", decision_id="d1", caps=caps))
    assert r1["ok"] and r1["capacity"]["auto_open"] == 1
    r2 = run(ar.reserve_entry(db, account_id=aid, user_id="u", source="crypto", decision_id="d2", caps=caps))
    assert r2["ok"] is False and r2["blocked"] == "max_concurrent_cap"        # 1 open + 1 reserved = cap 2
    assert "automated positions 2/2" in r2["violations"][0]
    run(ar.release(db, r1["reservation"]["reservation_id"], "refused:test"))
    r3 = run(ar.reserve_entry(db, account_id=aid, user_id="u", source="paper", decision_id="d3", caps=caps))
    assert r3["ok"]
    # daily capacity is PER SYMBOL (N97-1): the open XAUUSD trade + reservation count for XAUUSD only
    db.trades.rows[0]["symbol"] = "XAUUSD"
    r4 = run(ar.reserve_entry(db, account_id=aid, user_id="u", source="mt5", decision_id="d4", symbol="XAUUSD",
                              caps={"auto_cap": 0, "total_cap": 0, "daily_cap": 1}))
    assert r4["ok"] is False and r4["blocked"] == "trade_of_day_cap" and "1/1" in r4["violations"][0]


def test_guard_entry_presents_or_reserves_links_and_releases():
    import account_reservations as ar
    db = FakeDb()
    acc = {"_id": ObjectId()}
    aid = str(acc["_id"])

    async def ok_stage(rid):
        assert rid
        return {"id": "t1"}

    async def refused_stage(rid):
        return {"blocked": "safety_guardian"}
    out = run(ar.guard_entry(db, account=acc, user_id="u", signal={"symbol": "XAUUSD", "action": "BUY", "signal_id": "s1"},
                             source="mt5", max_concurrent=3, cfg_account_id=aid, run=ok_stage))
    assert out["id"] == "t1" and db.risk_reservations.rows[0]["state"] == "SLOT_LINKED" \
        and db.risk_reservations.rows[0]["trade_id"] == "t1" and db.risk_reservations.rows[0]["source"] == "mt5"
    out2 = run(ar.guard_entry(db, account=acc, user_id="u", signal={"symbol": "XAUUSD", "action": "SELL", "signal_id": "s2"},
                              source="mt5", max_concurrent=3, cfg_account_id=aid, run=refused_stage))
    assert out2["blocked"] == "safety_guardian" and db.risk_reservations.rows[1]["state"] == "RELEASED"
    # a presenting path (scalp) must present an ACTIVE reservation; it owns its own lifecycle
    out3 = run(ar.guard_entry(db, account=acc, user_id="u", signal={"_reservation_id": "nope"},
                              source="mt5", max_concurrent=3, cfg_account_id=aid, run=ok_stage))
    assert out3["blocked"] == "reservation_missing"
    from scalp import risk_reservations as rr
    presented = run(rr.reserve(db, account_id=aid, user_id="u", decision_id="scalp1", risk_usd=1, lot=0.01))
    out4 = run(ar.guard_entry(db, account=acc, user_id="u", signal={"_reservation_id": presented["reservation_id"]},
                              source="mt5", max_concurrent=1, cfg_account_id=aid, run=ok_stage))
    assert out4["id"] == "t1" and out4["reservation_id"] == presented["reservation_id"]
    assert [r for r in db.risk_reservations.rows if r["reservation_id"] == presented["reservation_id"]][0]["state"] == "RISK_RESERVED"


def test_every_engine_and_scalp_route_through_the_reservation_service():
    import execution
    from crypto_bridge.binance_engine import BinanceCCXTEngine
    for fn in (execution.MT5BridgeEngine.execute_authorized, execution.PaperEngine.execute, BinanceCCXTEngine.execute):
        assert "guard_entry(" in inspect.getsource(fn), fn
    assert 'trade_doc["reservation_id"] = reservation_id' in inspect.getsource(execution.MT5BridgeEngine._engine_stage)
    assert "count_documents(cap_q)" not in inspect.getsource(execution.MT5BridgeEngine._engine_stage)
    scalp_src = open(os.path.join(os.path.dirname(__file__), "..", "..", "scalp", "engine.py"), encoding="utf-8").read()
    assert '"_reservation_id": _resv["reservation_id"]' in scalp_src
    import seed
    assert "ensure_reservation_lock_indexes" in inspect.getsource(seed.ensure_indexes)


# ── P1-01 acceptance bundle + P0-02 release gate ─────────────────────────────
def test_acceptance_bundle_signed_verdict_and_authority_unlock():
    import acceptance_bundle as ab
    import trading_authority as ta
    db = FakeDb()
    acc_id = ObjectId()
    now = datetime.now(timezone.utc)
    acc = {"_id": acc_id, "mode": "live", "trading_enabled": True, "label": "Live-1", "status": "active",
           "last_heartbeat": now.isoformat(), "last_full_sync_at": now.isoformat(), "reconciliation_seq": 3,
           "ea_identity": {"installation_id": "inst1", "broker_server": "Broker-Live"}, "ea_version": "1.60"}
    db.accounts.rows.append(acc)
    db.bot_configs.rows.append({"_id": ObjectId(), "account_id": str(acc_id), "user_id": "u", "active": True})   # A14-9: 1 bot/account
    db.reconciliation_ledger.rows.append({"account_id": str(acc_id), "discrepancy": 0.0, "period_to": "2026-06-01", "seq": 1})
    full = {"level": "FULL", "domains": {"platform": {"level": "FULL", "reason": "x"},
                                         "acceptance": {"level": "CLOSE_ONLY", "reason": "no bundle"}}, "reasons": []}
    from test_fixplan_main99_phase4 import signer_env, approve_inventory
    # A14-10: production REFUSES in-process signing (external signer only), so the unit test signs in
    # preview mode with the local Ed25519 key and forces the gate on via ACCEPTANCE_BUNDLE_REQUIRED.
    env = {**signer_env(), "APP_ENV": "preview", "ACCEPTANCE_BUNDLE_REQUIRED": "true", "STOIC_IMAGE_DIGEST": "sha256:abc"}
    approve_inventory(db, acc_id)
    with patch.dict(os.environ, env), patch("trading_authority.compute_authority", AsyncMock(return_value=full)), \
            patch("modules.pamm.strategy_guard.GIT_COMMIT", "deadbeef1"), \
            patch("broker_env.attested_environment", lambda a: "LIVE"):
        b = run(ab.build_bundle(db, actor="admin@stoicaibot.com"))
        assert b["verdict"] == "PASS" and b["failures"] == [] and ab.verify_signature(b)
        assert b["image_digest"] == "sha256:abc" and b["build_sha"] == "deadbeef1" and str(acc_id) in b["account_ids"]
        ev = b["payload"]["accounts"][0]
        assert ev["environment"] == "LIVE" and ev["ea_session"]["fresh"] and ev["statement"]["status"] == "RECONCILED"
        assert "acceptance" not in " ".join(ev["authority"]["blockers"])   # the bundle is what unlocks it
        # tampering breaks the signature; a different release does not cover the account
        assert ab.verify_signature({**b, "payload": {**b["payload"], "actor": "mallory"}}) is False
        assert ab.bundle_covers(b, str(acc_id), {"build_sha": "deadbeef1", "image_digest": "sha256:abc"})[0] is True
        assert ab.bundle_covers(b, str(acc_id), {"build_sha": "other", "image_digest": "sha256:abc"})[0] is False
        assert ab.bundle_covers(b, "someone-else", {"build_sha": "deadbeef1", "image_digest": "sha256:abc"})[0] is False
        assert run(ta.acceptance_domain(db, acc))["level"] == "FULL"
        assert run(ta.acceptance_domain(db, {**acc, "_id": ObjectId()}))["level"] == "CLOSE_ONLY"
        assert run(ta.acceptance_domain(db, {**acc, "mode": "paper"}))["level"] == "FULL"
    # a FAIL bundle (stale EA session) never unlocks
    db.accounts.rows[0]["last_heartbeat"] = (now - timedelta(hours=2)).isoformat()
    db.acceptance_bundles.rows.clear()
    with patch.dict(os.environ, env), patch("trading_authority.compute_authority", AsyncMock(return_value=full)), \
            patch("modules.pamm.strategy_guard.GIT_COMMIT", "deadbeef1"), \
            patch("broker_env.attested_environment", lambda a: "LIVE"):
        b2 = run(ab.build_bundle(db, actor="admin"))
        assert b2["verdict"] == "FAIL" and any("EA session" in f for f in b2["failures"])
        assert run(ta.acceptance_domain(db, acc))["level"] == "CLOSE_ONLY"
    with patch.dict(os.environ, {"APP_ENV": "preview", "ACCEPTANCE_BUNDLE_REQUIRED": "false"}):
        assert run(ta.acceptance_domain(db, acc))["level"] == "FULL"       # not required outside production


def test_release_gate_blocks_non_authoritative_release_in_production():
    import release_gate as rg
    import trading_authority as ta
    dev_lock = {"authoritative": False, "images": {"backend": None, "frontend": None}, "git_commit": "4eba5af"}
    r = rg.evaluate(lock=dev_lock, env={}, ea_signed=False)
    assert r["ok"] is False and len(r["failures"]) == 4
    good_lock = {"authoritative": True, "images": {"backend": "sha256:be", "frontend": "sha256:fe"}, "git_commit": "abc"}
    assert rg.evaluate(lock=good_lock, env={"STOIC_IMAGE_DIGEST": "sha256:be"}, ea_signed=True)["ok"] is True
    bad = rg.evaluate(lock=good_lock, env={"STOIC_IMAGE_DIGEST": "sha256:other"}, ea_signed=True)
    assert bad["ok"] is False and "differs" in bad["failures"][0]
    with patch.dict(os.environ, {"APP_ENV": "production"}), patch("release_gate.evaluate", lambda **k: bad):
        d = run(ta.release_gate_domain(FakeDb()))
        assert d["level"] == "CLOSE_ONLY" and d["code"] == "RELEASE_NOT_AUTHORITATIVE"
    with patch.dict(os.environ, {"APP_ENV": "preview"}):
        assert run(ta.release_gate_domain(FakeDb()))["level"] == "FULL"
    assert ta.DOMAIN_SCOPE["release_gate"] == "platform_global" and ta.DOMAIN_SCOPE["acceptance"] == "account_bound"
    import deploy_preflight
    assert '"release_gate"' in inspect.getsource(deploy_preflight.run_preflight)
    summary = open(os.path.join(os.path.dirname(__file__), "..", "..", "..", "scripts", "generate_release_summary.py"), encoding="utf-8").read()
    assert '"RELEASABLE" if releasable' in summary and "images.get(\"backend\")" in summary
    page = open(os.path.join(os.path.dirname(__file__), "..", "..", "..", "frontend", "src", "pages", "DemoReadiness.jsx"), encoding="utf-8").read()
    assert "AcceptanceBundleCard" in page
