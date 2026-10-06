"""main99 review — Phase 3 (crypto stays OFF): A14-4 partial fills, A14-5 capability matrix,
A14-6 redacted errors, N99-9 finished OCO, recovered-fill fees, flatten-before-OCO, -2018/BadResponse."""
import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _client(**over):
    c = MagicMock()
    c.fetch_oco_status = AsyncMock(return_value=None)
    c.fetch_order_by_client_id = AsyncMock(return_value=None)
    c.fetch_order_trades = AsyncMock(return_value=[])
    c.cancel_order = AsyncMock(return_value={})
    c.amount_to_precision = lambda s, a: float(a)
    c.place_oco_protection = AsyncMock(return_value={"list_client_order_id": "x-oco", "tp_client_order_id": "x-tp",
                                                     "sl_client_order_id": "x-sl", "list_id": "9", "raw_status": "EXECUTING"})
    c.create_market_order = AsyncMock(return_value={"id": "fl1", "average": 100.0})
    for k, v in over.items():
        setattr(c, k, v)
    return c


def _intent(cid="stoic-abc"):
    return {"intent_id": "i1", "client_order_id": cid, "account_id": str(ObjectId()), "actor": "u",
            "payload": {"exchange_symbol": "BTC/USDT", "symbol": "BTCUSD", "side": "buy", "amount": 0.01,
                        "stop_loss": 90.0, "take_profit": 120.0, "signal_id": "s1"}}


# ── A14-4 — partial → partial → filled, partial → cancel, crash between updates ──
def test_a14_4_resting_partial_fill_is_exposure_then_remainder_cancelled_and_protected():
    import crypto_bridge.crypto_execution as cx
    db = FakeDb()
    it = _intent()
    fills = [{"amount": 0.004, "fee": {"currency": "BTC", "cost": 0.000004}}]
    client = _client(fetch_order_trades=AsyncMock(return_value=fills))
    o1 = {"id": "o1", "status": "open", "filled": 0.004, "amount": 0.01, "price": 100.0}
    st, recorded = run(cx._apply_exchange_truth(db, it, o1, client))
    row = db.trades.rows[0]
    assert st == "open" and recorded and row["status"] == "pending" and row["partial_fill"]
    assert abs(row["held_amount"] - 0.003996) < 1e-9                       # fee-aware, from the fills
    assert run(cx.unprotected_open_count(db)) == 1                           # pending + held ⇒ exposure counts
    # second partial update ("crash between updates" = the same truth re-applied) is idempotent
    fills.append({"amount": 0.003, "fee": {"currency": "BTC", "cost": 0.000003}})
    o2 = {**o1, "filled": 0.007}
    run(cx._apply_exchange_truth(db, it, o2, client))
    assert len(db.trades.rows) == 1 and abs(db.trades.rows[0]["held_amount"] - 0.006993) < 1e-9
    # sweep: cancel the remainder, then protect the FILLED amount
    acc = {"_id": ObjectId(it["account_id"])}
    db.accounts.rows.append(acc)
    db.trades.rows[0].update({"broker_kind": "binance", "account_id": it["account_id"], "action": "BUY",
                              "exchange_symbol": "BTC/USDT", "exchange_order_id": "o1", "stop_loss": 90.0, "take_profit": 120.0,
                              "protection": {"status": cx.PROTECTION_MISSING}})
    after_cancel = {"id": "o1", "status": "canceled", "filled": 0.007, "price": 100.0}
    client.fetch_order_by_client_id = AsyncMock(side_effect=lambda sym, cid: after_cancel if cid == it["client_order_id"] else None)

    class F:
        def __init__(self, a): pass
        async def __aenter__(self): return client
        async def __aexit__(self, *a): return False
    out = run(cx.reconcile_protection(db, F))
    t = db.trades.rows[0]
    assert client.cancel_order.await_count == 1 and t["status"] == "open" and t["remainder_cancelled_at"]
    assert t["protection"]["status"] == cx.PROTECTION_PLACED and out["protected"] == 1
    placed_qty = client.place_oco_protection.await_args.args[2]
    assert abs(placed_qty - 0.006993) < 1e-9                                 # protects what is HELD, nothing more


def test_a14_4_partial_then_cancel_with_zero_fill_is_cancelled_and_outcome_state():
    import crypto_bridge.crypto_execution as cx
    db = FakeDb()
    it = _intent("stoic-zero")
    run(cx._apply_exchange_truth(db, it, {"id": "o9", "status": "open", "filled": 0.0, "amount": 0.01, "price": 100.0}, _client()))
    assert db.trades.rows[0]["status"] == "pending" and db.trades.rows[0]["held_amount"] == 0 and run(cx.unprotected_open_count(db)) == 0
    run(cx._apply_exchange_truth(db, it, {"id": "o9", "status": "canceled", "filled": 0.0, "amount": 0.01}, _client()))
    assert db.trades.rows[0]["status"] == "cancelled"
    assert cx.outcome_state({"status": "canceled", "filled": 0.002}) == "filled"      # partial before cancel = position
    rec = cx.trade_from_exchange_order(it, {"id": "o2", "status": "open", "filled": 0.002, "price": 100.0})
    assert rec["status"] == "pending" and rec["partial_fill"] and rec["held_amount"] > 0


# ── N99-9 / flatten-before-OCO / cancelled-unfilled flatten ──────────────────
def test_n99_9_finished_oco_list_is_not_protection():
    import crypto_bridge.crypto_execution as cx
    db = FakeDb()
    oid = ObjectId()
    db.trades.rows.append({"_id": oid, "status": "open", "held_amount": 0.01, "entry_price": 100.0})
    # ALL_DONE with a filled TP leg → the position is closed, not "protected"
    legs = {"stoic-x-tp": {"status": "closed", "average": 120.0, "filled": 0.01}}
    client = _client(fetch_oco_status=AsyncMock(return_value={"listOrderStatus": "ALL_DONE", "orderListId": 5}),
                     fetch_order_by_client_id=AsyncMock(side_effect=lambda s, cid: legs.get(cid)))
    prot = run(cx.protect(db, client, trade_id=oid, ccxt_symbol="BTC/USDT", side="buy", amount=0.01,
                          stop_loss=90.0, take_profit=120.0, cid="stoic-x", account={}))
    assert prot["status"] == cx.PROTECTION_NA and prot["filled_leg"] == "tp"
    assert db.trades.rows[0]["status"] == "closed" and db.trades.rows[0]["close_reason"] == "oco_tp"
    assert client.place_oco_protection.await_count == 0
    # ALL_DONE without any fill → unprotected → flatten path (never re-adopted)
    db2 = FakeDb(); oid2 = ObjectId()
    db2.trades.rows.append({"_id": oid2, "status": "open", "held_amount": 0.01})
    client2 = _client(fetch_oco_status=AsyncMock(return_value={"listOrderStatus": "ALL_DONE"}))
    prot2 = run(cx.protect(db2, client2, trade_id=oid2, ccxt_symbol="BTC/USDT", side="buy", amount=0.01,
                           stop_loss=90.0, take_profit=120.0, cid="stoic-y", account={}))
    assert prot2["status"] == cx.PROTECTION_FLATTENED and client2.place_oco_protection.await_count == 0
    # EXECUTING list is adopted as before
    db3 = FakeDb(); oid3 = ObjectId(); db3.trades.rows.append({"_id": oid3, "status": "open"})
    client3 = _client(fetch_oco_status=AsyncMock(return_value={"listOrderStatus": "EXECUTING", "orderListId": 7}))
    assert run(cx.protect(db3, client3, trade_id=oid3, ccxt_symbol="BTC/USDT", side="buy", amount=0.01,
                          stop_loss=90.0, take_profit=120.0, cid="stoic-z", account={}))["adopted"] is True


def test_flatten_found_before_oco_and_cancelled_unfilled_flatten_is_not_a_flatten():
    import crypto_bridge.crypto_execution as cx
    db = FakeDb(); oid = ObjectId()
    db.trades.rows.append({"_id": oid, "status": "open", "held_amount": 0.01})
    prior = {"stoic-f-fl": {"id": "fl0", "status": "closed", "filled": 0.01, "average": 99.0}}
    client = _client(fetch_order_by_client_id=AsyncMock(side_effect=lambda s, cid: prior.get(cid)))
    prot = run(cx.protect(db, client, trade_id=oid, ccxt_symbol="BTC/USDT", side="buy", amount=0.01,
                          stop_loss=90.0, take_profit=120.0, cid="stoic-f", account={}))
    assert prot["status"] == cx.PROTECTION_FLATTENED and prot["adopted"] and client.place_oco_protection.await_count == 0
    assert db.trades.rows[0]["status"] == "closed"                        # the coins were already sold — no OCO over them
    db2 = FakeDb(); oid2 = ObjectId(); db2.trades.rows.append({"_id": oid2, "status": "open"})
    dead = {"stoic-g-fl": {"id": "fl1", "status": "canceled", "filled": 0.0}}
    client2 = _client(fetch_order_by_client_id=AsyncMock(side_effect=lambda s, cid: dead.get(cid)))
    out = run(cx._flatten_unprotected(db2, client2, oid2, "BTC/USDT", "buy", 0.01, "stoic-g", {"status": cx.PROTECTION_MISSING}))
    assert out["status"] == cx.PROTECTION_MISSING and out["flatten_error"] == "flatten_cancelled_unfilled"
    assert client2.create_market_order.await_count == 0 and db2.ops_alerts.rows[0]["severity"] == "critical"


# ── -2018 / BadResponse / load_markets ───────────────────────────────────────
def test_error_classification_2018_badresponse_and_load_markets_close():
    import crypto_bridge.crypto_execution as cx
    import crypto_bridge.ccxt_engine as ce
    import ccxt
    assert cx.never_left_exchange(ccxt.BadResponse("garbage")) is False        # UNKNOWN, not rejected
    assert cx.never_left_exchange(ccxt.InsufficientFunds("-2018 balance insufficient")) is True
    src = open(ce.__file__, encoding="utf-8").read()
    assert '"-2013" in str(e)' in src and '"-2018" in str(e)' not in src       # -2018 is NOT "order does not exist"
    ex = MagicMock(); ex.load_markets = AsyncMock(side_effect=RuntimeError("boom")); ex.close = AsyncMock()
    with patch.object(ce, "_new_exchange", lambda a: ex):
        with pytest.raises(RuntimeError):
            run(ce.CCXTClient({"exchange_id": "binance"}).__aenter__())
    assert ex.close.await_count == 1


# ── A14-5 / A14-6 ─────────────────────────────────────────────────────────────
def test_a14_5_capability_matrix_allows_live_only_on_binance():
    import crypto_bridge.ccxt_engine as ce
    m = ce.capability_matrix()
    assert m["binance"]["live_allowed"] is True and m["binance"]["missing"] == []
    for eid in ("kraken", "okx", "kucoin", "binanceus"):
        assert m[eid]["live_allowed"] is False and "oco_protection" in m[eid]["missing"], eid
    blk = ce.live_capability_block("kraken")
    assert blk["blocked"] == "exchange_not_certified" and "kraken" in blk["reason"]
    assert ce.live_capability_block("binance") is None
    eng = open(os.path.join(os.path.dirname(ce.__file__), "binance_engine.py"), encoding="utf-8").read()
    assert "live_capability_block(account.get" in eng
    jsx = open(os.path.join(os.path.dirname(ce.__file__), "..", "..", "frontend", "src", "pages", "Crypto.jsx"), encoding="utf-8").read()
    assert "exchange-capability-matrix" in jsx and "exchange-live-allowed" in jsx


def test_a14_6_no_raw_exchange_text_reaches_clients():
    import crypto_bridge.crypto_execution as cx
    import crypto_bridge.ccxt_engine as ce
    import ccxt
    red = ce.redact_exchange_error(ccxt.InvalidOrder("Filter failure: LOT_SIZE secret-detail"), "t")
    assert red["code"] == "exchange_rejected" and len(red["correlation_id"]) == 12 and "LOT_SIZE" not in str(red)
    out = cx.public_result({"blocked": "exchange_error", "error": "Binance {\"code\":-1013,\"msg\":\"Filter failure\"}",
                            "raw": {"secret": 1}, "protection": {"status": cx.PROTECTION_MISSING, "reason": "binance said no", "list_id": "x"}})
    assert out["error"] == "exchange_error" and "raw" not in out and out["protection"] == {"status": cx.PROTECTION_MISSING, "reason": "protection_failed"}
    db = FakeDb()
    db.execution_intents.rows.append({"intent_id": "i2", "status": "dispatched", "source": cx.SOURCE})
    res = run(cx.record_failure(db, "i2", ccxt.InvalidOrder("MIN_NOTIONAL blah")))
    assert res["error"] == "exchange_rejected" and "MIN_NOTIONAL" not in str(res) and res["correlation_id"]
    assert "redact_exchange_error" in open(ce.__file__, encoding="utf-8").read().split("def check_reachability")[1][:4000] \
        or "redact_exchange_error(e, f\"reachability" in open(ce.__file__, encoding="utf-8").read()
