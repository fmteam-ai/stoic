"""Crypto protective orders + lifecycle reconciliation — pure unit tests.

No network, no database: an in-memory collection double implements the
handful of motor calls the code uses (find/find_one/insert_one/update_one
with equality, $in and dotted $set), and the ccxt client is a MagicMock.
"""
import asyncio
import copy
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import ccxt.async_support as ccxt_async
import pytest
from bson import ObjectId

import crypto_lifecycle
from crypto_bridge import protective
from crypto_bridge.binance_engine import BinanceCCXTEngine, _post_dispatch_uncertain
from crypto_bridge.protective import (
    CLOSE_REASON_PROTECTION_FAILED, protect_or_flatten, protective_client_ids,
)

pytestmark = pytest.mark.unit


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ───────────────────────────── in-memory DB ─────────────────────────────

def _get(doc, dotted):
    cur = doc
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _matches(doc, q):
    for k, v in q.items():
        actual = _get(doc, k)
        if isinstance(v, dict) and "$in" in v:
            if actual not in v["$in"]:
                return False
        elif actual != v:
            return False
    return True


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    async def to_list(self, length=None):
        return [copy.deepcopy(d) for d in self._docs][:length]


class FakeCollection:
    def __init__(self):
        self.docs = []
        self.update_calls = []

    def find(self, q=None, *a, **k):
        return _Cursor([d for d in self.docs if _matches(d, q or {})])

    async def find_one(self, q=None, *a, **k):
        for d in self.docs:
            if _matches(d, q or {}):
                return copy.deepcopy(d)
        return None

    async def insert_one(self, doc):
        doc.setdefault("_id", ObjectId())
        self.docs.append(copy.deepcopy(doc))
        return MagicMock(inserted_id=doc["_id"])

    async def update_one(self, q, upd, *a, **k):
        self.update_calls.append((q, upd))
        for d in self.docs:
            if _matches(d, q):
                for key, val in (upd.get("$set") or {}).items():
                    parts = key.split(".")
                    tgt = d
                    for p in parts[:-1]:
                        tgt = tgt.setdefault(p, {})
                    tgt[parts[-1]] = copy.deepcopy(val)
                return MagicMock(modified_count=1, matched_count=1)
        return MagicMock(modified_count=0, matched_count=0)

    async def count_documents(self, q):
        return len([d for d in self.docs if _matches(d, q)])


class FakeDB:
    def __init__(self):
        self._c = {}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._c.setdefault(name, FakeCollection())


def _client(**over):
    c = MagicMock()
    c.load_markets = AsyncMock()
    c.is_contract = MagicMock(return_value=False)
    c.amount_to_precision = MagicMock(side_effect=lambda s, a: round(a, 8))
    c.create_binance_spot_oco = AsyncMock(return_value={
        "list_id": "L1", "sl": {"id": "SL1", "clientOrderId": "x"},
        "tp": {"id": "TP1", "clientOrderId": "y"}})
    c.create_okx_algo_oco = AsyncMock(return_value={"id": "ALGO1"})
    c.create_stop_loss_order = AsyncMock(return_value={"id": "SL1", "status": "open"})
    c.create_take_profit_limit_order = AsyncMock(return_value={"id": "TP1", "status": "open"})
    c.close_position_market = AsyncMock(return_value={
        "id": "FX1", "status": "closed", "average": 59950.0, "filled": 0.001})
    c.fetch_order = AsyncMock()
    c.fetch_order_by_client_id = AsyncMock(side_effect=ccxt_async.OrderNotFound("nf"))
    c.cancel_order = AsyncMock(return_value={"status": "canceled"})
    c.fetch_ticker = AsyncMock(return_value={"last": 60000.0})
    c.fetch_balance = AsyncMock(return_value={"total": {"BTC": 0.001}})
    for k, v in over.items():
        setattr(c, k, v)
    c.__aenter__ = AsyncMock(return_value=c)
    c.__aexit__ = AsyncMock(return_value=False)
    return c


def _account(**over):
    a = {"_id": ObjectId(), "user_id": "u1", "kind": "binance",
         "exchange_id": "binance", "testnet": True, "live": False,
         "trading_enabled": True, "equity": 10000.0, "balance": 10000.0,
         "creds": {"api_key": "x", "api_secret": "y"}}
    a.update(over)
    return a


def _trade(account, **over):
    t = {"_id": ObjectId(), "user_id": "u1", "account_id": str(account["_id"]),
         "symbol": "BTCUSD", "exchange_symbol": "BTC/USDT", "action": "BUY",
         "lot_size": 0.001, "entry_price": 60000.0, "stop_loss": 59700.0,
         "take_profit": 60900.0, "status": "open", "broker_kind": "binance",
         "exchange_order_id": "E1", "client_order_id": "stoicSIG123",
         "opened_at": datetime.now(timezone.utc).isoformat(),
         "protection": {"status": "pending"}}
    t.update(over)
    return t


@pytest.fixture(autouse=True)
def _quiet_side_effects(monkeypatch):
    monkeypatch.setattr("alerting.raise_alert", AsyncMock())
    monkeypatch.setattr("notifier.notify_trade_closed", AsyncMock(return_value=False))
    monkeypatch.setattr("notifier.notify_trade_opened", AsyncMock(return_value=False))
    fake_ws = MagicMock()
    fake_ws.broadcast = AsyncMock()
    monkeypatch.setattr("ws_manager.manager", fake_ws)
    monkeypatch.setattr("crypto_bridge.binance_engine.ws_manager", fake_ws)


# ───────────────────────── client ids ─────────────────────────

def test_protective_client_ids_are_deterministic_alnum_and_le_32():
    ids = protective_client_ids("65f0c0ffee0123456789abcd")
    assert ids["sl"] == "stoic65f0c0ffee0123456789abcdsl"
    assert ids["tp"] == "stoic65f0c0ffee0123456789abcdtp"
    long_ids = protective_client_ids("x" * 60)
    for v in list(ids.values()) + list(long_ids.values()):
        assert len(v) <= 32 and v.isalnum()
    assert protective_client_ids("abc") == protective_client_ids("abc")


# ─────────────────── engine: stop placed after fill ───────────────────

def _engine_patches(db, client, monkeypatch):
    monkeypatch.setattr("execution_authorization.verify_authorization",
                        lambda auth, iid: None)
    monkeypatch.setattr("trading_authority.gate_or_block", AsyncMock(return_value=None))
    monkeypatch.setattr("entitlements.verify_execution_entitlement",
                        AsyncMock(return_value=None))
    monkeypatch.setattr("order_authorization.authorize_order",
                        AsyncMock(return_value={"ok": True, "nonce": "n1"}))
    monkeypatch.setattr("crypto_bridge.binance_engine.get_db", lambda: db)
    monkeypatch.setattr("crypto_bridge.binance_engine.audit_pre_trade",
                        AsyncMock(return_value={"ok": True, "audit": [], "context": {}}))
    monkeypatch.setattr("crypto_bridge.binance_engine.BinanceClient",
                        MagicMock(return_value=client))
    monkeypatch.setattr("trade_explainer.snapshot_for_trade_creation",
                        AsyncMock(return_value=None))


def _signal(**over):
    s = {"symbol": "BTCUSD", "action": "BUY", "lot_size": 0.001,
         "entry_price": 60000.0, "stop_loss": 59700.0, "take_profit": 60900.0,
         "signal_id": "SIG123", "origin": "auto"}
    s.update(over)
    return s


def test_engine_places_binance_oco_after_market_fill(monkeypatch):
    db = FakeDB()
    client = _client(create_market_order=AsyncMock(return_value={
        "id": "E1", "status": "closed", "average": 60010.0, "filled": 0.001,
        "fees": [{"currency": "BTC", "cost": 0.000001}]}))
    _engine_patches(db, client, monkeypatch)
    out = _run(BinanceCCXTEngine().execute_authorized(
        user_id="u1", account=_account(), signal=_signal(),
        cfg_account_id="acc1", intent={"intent_id": "xin_1"}, authorization=object()))
    assert out["status"] == "open" and out["protection_outcome"] == "protected"
    client.create_binance_spot_oco.assert_awaited_once()
    args, kw = client.create_binance_spot_oco.await_args
    assert args[0] == "BTC/USDT" and args[1] == "sell"
    assert args[2] == pytest.approx(0.000999)          # net of base-asset fee
    assert args[3] == 59700.0 and args[4] == 60900.0
    assert kw["sl_client_order_id"] == "stoicSIG123sl"
    assert kw["tp_client_order_id"] == "stoicSIG123tp"
    stored = db.trades.docs[0]
    assert stored["protection"]["status"] == "placed"
    assert stored["protection"]["sl"]["order_id"] == "SL1"
    assert stored["confirmed_stop_loss"] == 59700.0
    assert stored["client_order_id"] == "stoicSIG123"


def test_engine_separate_stop_on_non_oco_venue_sell(monkeypatch):
    db = FakeDB()
    client = _client(
        create_market_order=AsyncMock(return_value={
            "id": "E2", "status": "closed", "average": 60000.0, "filled": 0.002}),
        create_take_profit_limit_order=AsyncMock(
            side_effect=ccxt_async.InsufficientFunds("balance locked")))
    _engine_patches(db, client, monkeypatch)
    out = _run(BinanceCCXTEngine().execute_authorized(
        user_id="u1", account=_account(exchange_id="kucoin"),
        signal=_signal(action="SELL", lot_size=0.002, stop_loss=60300.0,
                       take_profit=59000.0),
        intent={"intent_id": "xin_2"}, authorization=object()))
    assert out["status"] == "open"
    args, kw = client.create_stop_loss_order.await_args
    assert args == ("BTC/USDT", "buy", 0.002, 60300.0)
    assert kw["client_order_id"] == "stoicSIG123sl" and kw["reduce_only"] is False
    prot = db.trades.docs[0]["protection"]
    assert prot["status"] == "placed" and prot["soft_tp"] is True
    assert prot["sl"]["params"] == {"trigger": True}   # KuCoin stop orders
    client.close_position_market.assert_not_awaited()


def test_engine_stop_failure_flattens_and_closes(monkeypatch):
    db = FakeDB()
    client = _client(
        create_market_order=AsyncMock(return_value={
            "id": "E3", "status": "closed", "average": 60000.0, "filled": 0.001}),
        create_binance_spot_oco=AsyncMock(
            side_effect=ccxt_async.InvalidOrder("Stop price would trigger immediately")))
    _engine_patches(db, client, monkeypatch)
    out = _run(BinanceCCXTEngine().execute_authorized(
        user_id="u1", account=_account(), signal=_signal(),
        intent={"intent_id": "xin_3"}, authorization=object()))
    client.close_position_market.assert_awaited_once()
    args, kw = client.close_position_market.await_args
    assert args[:3] == ("BTC/USDT", "sell", 0.001)
    assert kw["client_order_id"] == "stoicSIG123fx"
    stored = db.trades.docs[0]
    assert stored["status"] == "closed"
    assert stored["close_reason"] == CLOSE_REASON_PROTECTION_FAILED
    assert stored["exit_price"] == 59950.0
    assert stored["pnl"] == pytest.approx(-0.05)
    assert stored["protection"]["status"] == "flattened"
    assert out["protection_outcome"] == "flattened"
    assert any(d["event"] == "stop_placement_failed"
               for d in db.crypto_protection_audit.docs)


def test_flatten_failure_keeps_trade_open_flagged_and_alerts():
    db = FakeDB()
    acc = _account(exchange_id="kraken")
    t = _trade(acc)
    _run(db.trades.insert_one(t))
    client = _client(
        create_stop_loss_order=AsyncMock(side_effect=ccxt_async.InvalidOrder("no")),
        close_position_market=AsyncMock(side_effect=ccxt_async.NetworkError("down")))
    import alerting
    res = _run(protect_or_flatten(db, client, acc, t, amount=0.001))
    assert res["outcome"] == "flatten_failed"
    stored = db.trades.docs[0]
    assert stored["status"] == "open"
    assert stored["protection"]["status"] == "flatten_failed"
    assert stored["protection"]["flatten_attempts"] == 1
    alerting.raise_alert.assert_awaited()


def test_lost_stop_response_is_recovered_not_flattened():
    db = FakeDB()
    acc = _account(exchange_id="kraken")
    t = _trade(acc, take_profit=None)
    _run(db.trades.insert_one(t))
    client = _client(
        create_stop_loss_order=AsyncMock(side_effect=ccxt_async.RequestTimeout("t/o")),
        fetch_order_by_client_id=AsyncMock(return_value={"id": "SLX", "status": "open"}))
    res = _run(protect_or_flatten(db, client, acc, t, amount=0.001))
    assert res["outcome"] == "protected"
    client.close_position_market.assert_not_awaited()
    assert db.trades.docs[0]["protection"]["sl"]["order_id"] == "SLX"


def test_engine_uncertain_entry_persists_pending_not_blocked(monkeypatch):
    db = FakeDB()
    client = _client(create_market_order=AsyncMock(
        side_effect=ccxt_async.RequestTimeout("read timeout")))
    _engine_patches(db, client, monkeypatch)
    out = _run(BinanceCCXTEngine().execute_authorized(
        user_id="u1", account=_account(), signal=_signal(),
        intent={"intent_id": "xin_4"}, authorization=object()))
    assert "blocked" not in out
    stored = db.trades.docs[0]
    assert stored["status"] == "pending"
    assert stored["exchange_order_status"] == "unknown"
    client.create_binance_spot_oco.assert_not_awaited()


def test_engine_definite_exchange_error_is_blocked(monkeypatch):
    db = FakeDB()
    client = _client(create_market_order=AsyncMock(
        side_effect=ccxt_async.InsufficientFunds("insufficient funds")))
    _engine_patches(db, client, monkeypatch)
    out = _run(BinanceCCXTEngine().execute_authorized(
        user_id="u1", account=_account(), signal=_signal(),
        intent={"intent_id": "xin_5"}, authorization=object()))
    assert out["blocked"] == "exchange_error"
    assert db.trades.docs == []


def test_post_dispatch_classification():
    assert _post_dispatch_uncertain(ccxt_async.RequestTimeout("x")) is True
    assert _post_dispatch_uncertain(ccxt_async.NetworkError("x")) is True
    assert _post_dispatch_uncertain(ccxt_async.InvalidOrder("x")) is False
    assert _post_dispatch_uncertain(RuntimeError("refused")) is False


def test_engine_refuses_without_authorization():
    out = _run(BinanceCCXTEngine().execute_authorized(
        user_id="u1", account=_account(), signal=_signal(),
        intent={"intent_id": "xin_6"}, authorization=None))
    assert out["blocked"] == "unauthorized_execution_path"


def test_engine_requires_stop_loss(monkeypatch):
    db = FakeDB()
    client = _client(create_market_order=AsyncMock())
    _engine_patches(db, client, monkeypatch)
    out = _run(BinanceCCXTEngine().execute_authorized(
        user_id="u1", account=_account(), signal=_signal(stop_loss=0),
        intent={"intent_id": "xin_7"}, authorization=object()))
    assert out["blocked"] == "crypto_stop_required"
    client.create_market_order.assert_not_awaited()


def test_execute_routes_through_execution_authority(monkeypatch):
    seen = {}

    async def fake_submit(**kw):
        seen.update(kw)
        return {"blocked": "trading_authority"}
    monkeypatch.setattr("execution_authority.submit_intent", fake_submit)
    eng = BinanceCCXTEngine()
    out = _run(eng.execute(user_id="u1", account=_account(), signal=_signal(),
                           max_concurrent=3, cfg_account_id="acc1"))
    assert out == {"blocked": "trading_authority"}
    assert seen["engine"] is eng and seen["max_concurrent"] == 3
    assert seen["signal"]["_crypto_requested_amount"] == 0.001


def test_authority_reduced_never_inflates_crypto_amount(monkeypatch):
    db = FakeDB()
    client = _client(create_market_order=AsyncMock(return_value={
        "id": "E8", "status": "closed", "average": 60000.0, "filled": 0.0005}))
    _engine_patches(db, client, monkeypatch)
    # submit_intent's REDUCED branch does max(0.01, round(0.001*0.5, 2)) = 0.01
    sig = _signal(lot_size=0.01, _authority_reduced=True, _crypto_requested_amount=0.001)
    _run(BinanceCCXTEngine().execute_authorized(
        user_id="u1", account=_account(), signal=sig,
        intent={"intent_id": "xin_8"}, authorization=object()))
    assert client.create_market_order.await_args[0][2] == pytest.approx(0.0005)


# ───────────────────────────── lifecycle sweep ─────────────────────────────

def _sweep(db, client):
    return _run(crypto_lifecycle.sweep_once(db, client_factory=lambda acc: client))


def _setup(trade_over=None, acc_over=None):
    db = FakeDB()
    acc = _account(**(acc_over or {}))
    _run(db.accounts.insert_one(acc))
    t = _trade(acc, **(trade_over or {}))
    _run(db.trades.insert_one(t))
    return db, acc, t


def _placed(strategy="separate", oco=False, tp=True):
    p = {"status": "placed", "strategy": strategy, "oco": oco, "amount": 0.001,
         "side": "sell", "soft_tp": False,
         "sl": {"order_id": "SL1", "client_order_id": "stoicSIG123sl",
                "price": 59700.0, "params": {}},
         "tp": ({"order_id": "TP1", "client_order_id": "stoicSIG123tp",
                 "price": 60900.0, "params": {}} if tp else None)}
    return p


def test_sweep_entry_fill_opens_and_protects():
    db, acc, t = _setup({"status": "pending", "exchange_order_id": "E1",
                         "entry_price": 59500.0})
    client = _client(fetch_order=AsyncMock(return_value={
        "id": "E1", "status": "closed", "average": 59480.0, "filled": 0.001}))
    out = _sweep(db, client)
    stored = db.trades.docs[0]
    assert out["filled"] == 1
    assert stored["status"] == "open"
    assert stored["entry_price"] == 59480.0
    assert stored["protection"]["status"] == "placed"
    client.create_binance_spot_oco.assert_awaited_once()


def test_sweep_entry_cancelled_without_fill():
    db, acc, t = _setup({"status": "pending"})
    client = _client(fetch_order=AsyncMock(return_value={
        "id": "E1", "status": "canceled", "filled": 0}))
    out = _sweep(db, client)
    assert out["cancelled"] == 1
    assert db.trades.docs[0]["status"] == "cancelled"
    client.create_binance_spot_oco.assert_not_awaited()


def test_sweep_lost_entry_not_found_after_grace_is_cancelled():
    old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    db, acc, t = _setup({"status": "pending", "exchange_order_id": "",
                         "opened_at": old})
    client = _client()
    _sweep(db, client)
    stored = db.trades.docs[0]
    assert stored["status"] == "cancelled"
    assert stored["close_reason"] == "entry_not_found"


def test_sweep_sl_fill_closes_trade_with_pnl_and_cancels_tp():
    db, acc, t = _setup({"protection": _placed()})
    orders = {"SL1": {"id": "SL1", "status": "closed", "average": 59690.0,
                      "filled": 0.001,
                      "fees": [{"currency": "USDT", "cost": 0.06}]},
              "TP1": {"id": "TP1", "status": "open"}}
    client = _client(fetch_order=AsyncMock(side_effect=lambda oid, s, p=None: orders[oid]))
    out = _sweep(db, client)
    stored = db.trades.docs[0]
    assert stored["status"] == "closed" and stored["close_reason"] == "sl"
    assert stored["exit_price"] == 59690.0
    assert stored["pnl"] == pytest.approx((59690.0 - 60000.0) * 0.001 - 0.06)
    assert out["closed"] == 1
    client.cancel_order.assert_awaited_once_with("TP1", "BTC/USDT", {})


def test_sweep_tp_fill_cancels_sibling_stop():
    db, acc, t = _setup({"protection": _placed()})
    orders = {"SL1": {"id": "SL1", "status": "open"},
              "TP1": {"id": "TP1", "status": "closed", "average": 60900.0,
                      "filled": 0.001}}
    client = _client(fetch_order=AsyncMock(side_effect=lambda oid, s, p=None: orders[oid]))
    out = _sweep(db, client)
    stored = db.trades.docs[0]
    assert stored["close_reason"] == "tp"
    assert stored["pnl"] == pytest.approx(0.9)
    client.cancel_order.assert_awaited_once_with("SL1", "BTC/USDT", {})
    assert out["siblings_cancelled"] == 1


def test_sweep_oco_tp_fill_does_not_cancel_sibling():
    db, acc, t = _setup({"protection": _placed("binance_oco", oco=True)})
    orders = {"SL1": {"id": "SL1", "status": "expired"},
              "TP1": {"id": "TP1", "status": "closed", "average": 60900.0}}
    client = _client(fetch_order=AsyncMock(side_effect=lambda oid, s, p=None: orders[oid]))
    _sweep(db, client)
    assert db.trades.docs[0]["close_reason"] == "tp"
    client.cancel_order.assert_not_awaited()


def test_sweep_is_idempotent_second_pass_noop():
    db, acc, t = _setup({"protection": _placed()})
    orders = {"SL1": {"id": "SL1", "status": "open"},
              "TP1": {"id": "TP1", "status": "closed", "average": 60900.0}}
    client = _client(fetch_order=AsyncMock(side_effect=lambda oid, s, p=None: orders[oid]))
    first = _sweep(db, client)
    snapshot = copy.deepcopy(db.trades.docs[0])
    n_updates = len(db.trades.update_calls)
    second = _sweep(db, client)
    assert first["closed"] == 1 and second["closed"] == 0
    assert second["checked"] == 0
    assert db.trades.docs[0] == snapshot
    assert len(db.trades.update_calls) == n_updates
    assert client.cancel_order.await_count == 1


def test_sweep_status_guard_never_overwrites_concurrent_close():
    db, acc, t = _setup({"protection": _placed()})

    async def fetch(oid, s, p=None):
        # another writer closes the trade between the sweep's read and write
        db.trades.docs[0].update({"status": "closed", "close_reason": "manual",
                                  "exit_price": 1.0})
        return {"id": oid, "status": "closed", "average": 59690.0}
    client = _client(fetch_order=AsyncMock(side_effect=fetch))
    out = _sweep(db, client)
    stored = db.trades.docs[0]
    assert stored["close_reason"] == "manual" and stored["exit_price"] == 1.0
    assert out["closed"] == 0
    guard_q = db.trades.update_calls[-1][0]
    assert guard_q["status"] == "open" and guard_q["_id"] == t["_id"]
    client.cancel_order.assert_not_awaited()


def test_sweep_unprotected_open_trade_gets_protected():
    db, acc, t = _setup({"protection": {"status": "pending"}},
                        acc_over={"exchange_id": "okx"})
    client = _client()
    _sweep(db, client)
    client.create_okx_algo_oco.assert_awaited_once()
    args, kw = client.create_okx_algo_oco.await_args
    assert args == ("BTC/USDT", "sell", 0.001, 59700.0, 60900.0)
    assert kw["client_order_id"] == "stoicSIG123ol"
    prot = db.trades.docs[0]["protection"]
    assert prot["status"] == "placed" and prot["single_order"] is True


def test_sweep_stop_cancelled_externally_with_position_gone_closes():
    db, acc, t = _setup({"protection": _placed()})
    orders = {"SL1": {"id": "SL1", "status": "canceled"},
              "TP1": {"id": "TP1", "status": "open"}}
    client = _client(fetch_order=AsyncMock(side_effect=lambda oid, s, p=None: orders[oid]),
                     fetch_balance=AsyncMock(return_value={"total": {"BTC": 0.0}}),
                     fetch_ticker=AsyncMock(return_value={"last": 60100.0}))
    _sweep(db, client)
    stored = db.trades.docs[0]
    assert stored["status"] == "closed" and stored["close_reason"] == "external_close"
    assert stored["exit_price"] == 60100.0


def test_sweep_stop_cancelled_externally_position_held_reprotects():
    db, acc, t = _setup({"protection": _placed()})
    orders = {"SL1": {"id": "SL1", "status": "canceled"},
              "TP1": {"id": "TP1", "status": "open"}}
    client = _client(fetch_order=AsyncMock(side_effect=lambda oid, s, p=None: orders[oid]))
    _sweep(db, client)
    stored = db.trades.docs[0]
    assert stored["status"] == "open"
    client.cancel_order.assert_awaited_once_with("TP1", "BTC/USDT", {})  # free locked balance
    kw = client.create_binance_spot_oco.await_args.kwargs
    assert kw["sl_client_order_id"] != "stoicSIG123sl"   # fresh deterministic id
    assert len(kw["sl_client_order_id"]) <= 32
    assert stored["protection"]["reprotect_count"] == 1


def test_sweep_soft_tp_crossed_cancels_stop_and_closes():
    prot = _placed(tp=False)
    prot["soft_tp"] = True
    db, acc, t = _setup({"protection": prot}, acc_over={"exchange_id": "kraken"})
    client = _client(
        fetch_order=AsyncMock(side_effect=[{"id": "SL1", "status": "open"},
                                           {"id": "SL1", "status": "canceled"}]),
        fetch_ticker=AsyncMock(return_value={"last": 61000.0}),
        close_position_market=AsyncMock(return_value={
            "id": "FX", "status": "closed", "average": 60990.0}))
    _sweep(db, client)
    stored = db.trades.docs[0]
    assert stored["status"] == "closed" and stored["close_reason"] == "tp"
    client.cancel_order.assert_awaited_once_with("SL1", "BTC/USDT", {})
    assert stored["exit_price"] == 60990.0


def test_run_loop_stops_on_event(monkeypatch):
    calls = []

    async def fake_sweep(db):
        calls.append(1)
        stop.set()
        return {"checked": 0}
    monkeypatch.setattr(crypto_lifecycle, "sweep_once", fake_sweep)
    monkeypatch.setattr("database.get_db", lambda: None)
    loop = asyncio.new_event_loop()
    stop = asyncio.Event()
    loop.run_until_complete(asyncio.wait_for(
        crypto_lifecycle.run_loop(stop_event=stop, interval_s=0.01), 2))
    assert calls == [1]


def test_protective_module_is_spot_and_contract_aware():
    # contracts → reduceOnly separate orders, never spot OCO
    db = FakeDB()
    acc = _account()
    t = _trade(acc)
    _run(db.trades.insert_one(t))
    client = _client(is_contract=MagicMock(return_value=True))
    _run(protective.protect_or_flatten(db, client, acc, t, amount=0.001))
    client.create_binance_spot_oco.assert_not_awaited()
    assert client.create_stop_loss_order.await_args.kwargs["reduce_only"] is True
    assert client.create_take_profit_limit_order.await_args.kwargs["reduce_only"] is True
