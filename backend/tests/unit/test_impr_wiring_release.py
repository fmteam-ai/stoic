"""impr-wiring — exposure reservations are released on every close / cancel
path, the crypto engine reserves before its insert, and the learned-meta /
drift / meta-labeling retrains are scoped to the owner.

Pure unit tests: an in-memory motor-like DB double (async find_one /
find_one_and_update / update_one / update_many / insert_one / find cursor)
drives the REAL execution_authority.reserve_exposure / release_reservation
so the assertions are on the stored counters, not on mocks alone.
"""
import asyncio
import copy
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from bson import ObjectId

import execution_authority as ea

pytestmark = pytest.mark.unit


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ───────────────────────────── in-memory DB ─────────────────────────────

def _get(doc, dotted):
    cur = doc
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None, False
        cur = cur[part]
    return cur, True


def _match(doc, flt):
    for k, cond in (flt or {}).items():
        if k == "$or":
            if not any(_match(doc, f) for f in cond):
                return False
            continue
        if k == "$and":
            if not all(_match(doc, f) for f in cond):
                return False
            continue
        val, present = _get(doc, k)
        if isinstance(cond, dict) and cond and all(c.startswith("$") for c in cond):
            for op, arg in cond.items():
                if op == "$lt" and not (present and val is not None and val < arg):
                    return False
                if op == "$lte" and not (present and val is not None and val <= arg):
                    return False
                if op == "$gte" and not (present and val is not None and val >= arg):
                    return False
                if op == "$in" and val not in arg:
                    return False
                if op == "$ne" and val == arg:
                    return False
                if op == "$exists" and present != bool(arg):
                    return False
        elif val != cond:
            return False
    return True


def _apply(doc, upd):
    for k, v in (upd.get("$inc") or {}).items():
        parts = k.split(".")
        cur = doc
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = cur.get(parts[-1], 0) + v
    for k, v in (upd.get("$set") or {}).items():
        parts = k.split(".")
        cur = doc
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = copy.deepcopy(v)
    for k in (upd.get("$unset") or {}):
        parts = k.split(".")
        cur = doc
        for p in parts[:-1]:
            cur = cur.get(p, {})
        cur.pop(parts[-1], None)


class _Res:
    def __init__(self, n, inserted_id=None):
        self.modified_count = n
        self.matched_count = n
        self.inserted_id = inserted_id


class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    def sort(self, *a, **k):
        return self

    def limit(self, n):
        self._rows = self._rows[:n]
        return self

    async def to_list(self, length=None):
        rows = [copy.deepcopy(r) for r in self._rows]
        return rows if length is None else rows[:length]

    def __aiter__(self):
        self._it = iter([copy.deepcopy(r) for r in self._rows])
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class Coll:
    def __init__(self):
        self.docs = []

    def find(self, q=None, *a, **k):
        return _Cursor([d for d in self.docs if _match(d, q or {})])

    async def find_one(self, q=None, *a, **k):
        for d in self.docs:
            if _match(d, q or {}):
                return copy.deepcopy(d)
        return None

    async def find_one_and_update(self, q, upd, *a, **k):
        for d in self.docs:
            if _match(d, q):
                _apply(d, upd)
                return copy.deepcopy(d)
        return None

    async def insert_one(self, doc, *a, **k):
        doc.setdefault("_id", ObjectId())
        if any(d.get("_id") == doc["_id"] for d in self.docs):
            raise Exception("duplicate key")
        self.docs.append(copy.deepcopy(doc))
        return _Res(1, doc["_id"])

    async def update_one(self, q, upd, *a, upsert=False, **k):
        for d in self.docs:
            if _match(d, q):
                _apply(d, upd)
                return _Res(1)
        if upsert:
            nd = {kk: vv for kk, vv in q.items() if not kk.startswith("$")}
            _apply(nd, upd)
            self.docs.append(nd)
        return _Res(0)

    async def update_many(self, q, upd, *a, **k):
        n = 0
        for d in self.docs:
            if _match(d, q):
                _apply(d, upd)
                n += 1
        return _Res(n)

    async def count_documents(self, q, *a, **k):
        return len([d for d in self.docs if _match(d, q)])


class DB:
    def __init__(self):
        self._c = {}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._c.setdefault(name, Coll())

    def __getitem__(self, name):
        return getattr(self, name)


def _slots(db):
    return {d["_id"]: d.get("total_slots") for d in db.exposure_reservations.docs}


async def _reserved_trade(db, *, status="open", user="u1", acct=None, **over):
    acct = acct or str(ObjectId())
    res = await ea.reserve_exposure(
        db, user_id=user, account_id=acct, symbol="XAUUSD", lot=0.1,
        risk_usd=25.0, auto=True, max_concurrent=3)
    assert res["ok"] and res["reservation"]
    t = {"_id": ObjectId(), "user_id": user, "account_id": acct,
         "symbol": "XAUUSD", "action": "BUY", "lot_size": 0.1,
         "entry_price": 2000.0, "stop_loss": 1990.0, "status": status,
         "mt5_ticket": 111, "origin": "auto",
         "opened_at": "2020-01-01T00:00:00+00:00",
         "exposure_reservation": res["reservation"]}
    t.update(over)
    await db.trades.insert_one(t)
    return t


@pytest.fixture
def release_spy(monkeypatch):
    calls = []
    real = ea.release_reservation

    async def spy(db, t):
        calls.append(t)
        return await real(db, t)
    monkeypatch.setattr(ea, "release_reservation", spy)
    return calls


# ───────────────────────────── bridge /report ─────────────────────────────

def _report_patches(monkeypatch, db, acc):
    import routes.bridge_routes as br
    monkeypatch.setattr(br, "get_db", lambda: db)
    monkeypatch.setattr(br, "_account_by_token", AsyncMock(return_value=acc))
    ws = MagicMock()
    ws.broadcast = AsyncMock()
    monkeypatch.setattr(br, "ws_manager", ws)
    monkeypatch.setattr("notifier.notify_trade_closed", AsyncMock(return_value=False))
    monkeypatch.setattr(asyncio, "create_task", lambda coro, *a, **k: coro.close())
    return br


def test_report_close_releases_once(monkeypatch, release_spy):
    db = DB()
    acc = {"_id": ObjectId(), "user_id": "u1"}
    t = _run(_reserved_trade(db, acct=str(acc["_id"])))
    assert set(_slots(db).values()) == {1}
    br = _report_patches(monkeypatch, db, acc)
    from models import BridgeTradeReport
    p = BridgeTradeReport(bridge_token="tok", trade_id=str(t["_id"]),
                          status="closed", exit_price=2010.0, pnl=10.0)
    _run(br.report_trade(p))
    assert len(release_spy) == 1
    assert set(_slots(db).values()) == {0}
    # replayed close report: release is idempotent — counters never go < 0
    _run(br.report_trade(p))
    assert set(_slots(db).values()) == {0}


def test_report_open_does_not_release(monkeypatch, release_spy):
    db = DB()
    acc = {"_id": ObjectId(), "user_id": "u1"}
    t = _run(_reserved_trade(db, acct=str(acc["_id"]), status="pending",
                             mt5_ticket=None))
    br = _report_patches(monkeypatch, db, acc)
    monkeypatch.setattr(br, "inc_intel_counter", AsyncMock())
    from models import BridgeTradeReport
    _run(br.report_trade(BridgeTradeReport(
        bridge_token="tok", trade_id=str(t["_id"]), status="open",
        mt5_ticket=111)))
    assert release_spy == []
    assert set(_slots(db).values()) == {1}


def test_stale_pending_cancel_releases_each_cancelled_doc(release_spy):
    import routes.bridge_routes as br
    db = DB()
    acct = str(ObjectId())
    t = _run(_reserved_trade(db, acct=acct, status="pending", mt5_ticket=None))
    n = _run(br._cancel_stale_pending_opens(db, acct, "2099-01-01T00:00:00"))
    assert n == 1
    assert [str(r["_id"]) for r in release_spy] == [str(t["_id"])]
    assert set(_slots(db).values()) == {0}


# ───────────────────────────── reconciler ─────────────────────────────

def _recon_patches(monkeypatch, db):
    import trade_reconciler as tr
    monkeypatch.setattr(tr, "get_db", lambda: db)
    ws = MagicMock()
    ws.broadcast = AsyncMock()
    monkeypatch.setattr(tr, "ws_manager", ws)
    return tr


def test_reconciler_close_releases(monkeypatch, release_spy):
    db = DB()
    t = _run(_reserved_trade(db))
    tr = _recon_patches(monkeypatch, db)
    out = _run(tr.reconcile_account(t["account_id"], [], source="heartbeat"))
    assert out["closed_count"] == 1
    assert len(release_spy) == 1
    assert set(_slots(db).values()) == {0}


def test_status_guarded_no_match_does_not_release(monkeypatch, release_spy):
    db = DB()
    t = _run(_reserved_trade(db))
    tr = _recon_patches(monkeypatch, db)
    # a concurrent EA close report won the race → guarded update matches 0
    db.trades.update_one = AsyncMock(return_value=_Res(0))
    out = _run(tr.reconcile_account(t["account_id"], [], source="heartbeat"))
    assert out["closed_count"] == 0
    assert release_spy == []
    assert set(_slots(db).values()) == {1}


# ───────────────────────────── panic ─────────────────────────────

def test_panic_cancel_releases(monkeypatch, release_spy):
    import routes.panic_routes as pr
    db = DB()
    t = _run(_reserved_trade(db, status="pending", mt5_ticket=None))
    other = _run(_reserved_trade(db, user="u2", status="pending", mt5_ticket=None))
    monkeypatch.setattr(pr, "get_db", lambda: db)
    monkeypatch.setattr(pr, "publish_panic_outbox", AsyncMock(return_value=True))
    monkeypatch.setattr("close_commands.request_close", AsyncMock(return_value={
        "trades_marked_for_close": 0, "command_id": None}))
    monkeypatch.setattr("canonical_decision.bump_authority_version",
                        AsyncMock(return_value=1))
    out = _run(pr._disable_all_bots_and_close_trades(
        {"user_id": "u1"}, broadcast_user_id="u1"))
    assert out["trades_cancelled"] == 1
    assert [str(r["_id"]) for r in release_spy] == [str(t["_id"])]
    slots = _slots(db)
    assert slots[f"acct:{t['account_id']}"] == 0
    assert slots[f"acct:{other['account_id']}"] == 1   # other user untouched


# ───────────────────────────── crypto lifecycle ─────────────────────────────

def test_crypto_lifecycle_sl_fill_releases(monkeypatch, release_spy):
    import crypto_lifecycle as cl
    monkeypatch.setattr(cl, "_notify_closed", AsyncMock())
    db = DB()
    t = _run(_reserved_trade(db, broker_kind="binance", mt5_ticket=None,
                             protection={"status": "placed", "amount": 0.1}))
    stats = {"closed": 0}
    assert _run(cl._close(db, t, exit_price=1990.0, reason="sl", stats=stats))
    assert len(release_spy) == 1
    assert set(_slots(db).values()) == {0}
    # second close of the same row: guarded update misses → no release
    assert not _run(cl._close(db, t, exit_price=1990.0, reason="sl", stats=stats))
    assert len(release_spy) == 1


# ───────────────────────────── binance engine ─────────────────────────────

def _engine_patches(db, monkeypatch):
    from crypto_bridge import binance_engine as be
    client = MagicMock()
    client.create_market_order = AsyncMock(return_value={
        "id": "E1", "status": "closed", "average": 60000.0, "filled": 0.001})
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("execution_authorization.verify_authorization",
                        lambda auth, iid: None)
    monkeypatch.setattr("trading_authority.gate_or_block", AsyncMock(return_value=None))
    monkeypatch.setattr("entitlements.verify_execution_entitlement",
                        AsyncMock(return_value=None))
    monkeypatch.setattr("order_authorization.authorize_order",
                        AsyncMock(return_value={"ok": True, "nonce": "n1"}))
    monkeypatch.setattr("crypto_bridge.ccxt_engine.orders_blocked_reason",
                        lambda acc: None)
    monkeypatch.setattr(be, "get_db", lambda: db)
    monkeypatch.setattr(be, "audit_pre_trade",
                        AsyncMock(return_value={"ok": True, "audit": [], "context": {}}))
    monkeypatch.setattr(be, "BinanceClient", MagicMock(return_value=client))
    monkeypatch.setattr("crypto_bridge.protective.protect_or_flatten",
                        AsyncMock(return_value={"outcome": "protected"}))
    ws = MagicMock()
    ws.broadcast = AsyncMock()
    monkeypatch.setattr(be, "ws_manager", ws)
    monkeypatch.setattr("notifier.notify_trade_opened", AsyncMock(return_value=False))
    return be, client


def _crypto_account():
    return {"_id": ObjectId(), "user_id": "u1", "kind": "binance",
            "exchange_id": "binance", "testnet": True, "live": False,
            "equity": 10000.0, "balance": 10000.0}


def _crypto_signal():
    return {"symbol": "BTCUSD", "action": "BUY", "lot_size": 0.001,
            "entry_price": 60000.0, "stop_loss": 59700.0,
            "take_profit": 60900.0, "signal_id": "SIG1", "origin": "auto"}


def test_binance_engine_reserves_before_insert(monkeypatch):
    db = DB()
    be, client = _engine_patches(db, monkeypatch)
    acc = _crypto_account()
    seen = {}
    real_insert = db.trades.insert_one

    async def insert(doc, *a, **k):
        seen["slots_at_insert"] = _slots(db)
        return await real_insert(doc)
    db.trades.insert_one = insert
    out = _run(be.BinanceCCXTEngine().execute_authorized(
        user_id="u1", account=acc, signal=_crypto_signal(),
        max_concurrent=2, intent={"intent_id": "x1"}, authorization=object()))
    assert out["status"] == "open"
    assert set(seen["slots_at_insert"].values()) == {1}
    stored = db.trades.docs[0]
    rsv = stored["exposure_reservation"]
    # crypto risk = amount × |entry − stop| = 0.001 × 300 = $0.30, not lots×pips
    assert rsv["risk_usd"] == pytest.approx(0.3)
    hold = next(iter(db.exposure_reservations.docs[0]["holds"].values()))
    assert hold["lot"] == pytest.approx(0.001)


def test_binance_engine_reservation_block_never_sends_order(monkeypatch):
    db = DB()
    be, client = _engine_patches(db, monkeypatch)
    monkeypatch.setattr("execution_authority.reserve_exposure", AsyncMock(
        return_value={"ok": False, "blocked": "max_concurrent_cap"}))
    out = _run(be.BinanceCCXTEngine().execute_authorized(
        user_id="u1", account=_crypto_account(), signal=_crypto_signal(),
        max_concurrent=2, intent={"intent_id": "x1"}, authorization=object()))
    assert out["blocked"] == "max_concurrent_cap"
    client.create_market_order.assert_not_awaited()
    assert db.trades.docs == []


def test_binance_engine_releases_on_insert_failure(monkeypatch):
    db = DB()
    be, client = _engine_patches(db, monkeypatch)

    async def boom(*a, **k):
        raise RuntimeError("insert failed")
    monkeypatch.setattr(be.BinanceCCXTEngine, "_enrich_and_insert",
                        staticmethod(boom))
    _run(be.BinanceCCXTEngine().execute_authorized(
        user_id="u1", account=_crypto_account(), signal=_crypto_signal(),
        max_concurrent=2, intent={"intent_id": "x1"}, authorization=object()))
    assert db.trades.docs == []
    assert set(_slots(db).values()) == {0}


def test_binance_engine_releases_on_exchange_error(monkeypatch):
    db = DB()
    be, client = _engine_patches(db, monkeypatch)
    client.create_market_order = AsyncMock(side_effect=ValueError("bad symbol"))
    out = _run(be.BinanceCCXTEngine().execute_authorized(
        user_id="u1", account=_crypto_account(), signal=_crypto_signal(),
        max_concurrent=2, intent={"intent_id": "x1"}, authorization=object()))
    assert out["blocked"] == "exchange_error"
    assert set(_slots(db).values()) == {0}


# ───────────────────────────── ML wiring ─────────────────────────────

def test_analytics_retrain_passes_user_id(monkeypatch):
    import routes.analytics_routes as ar
    spy = AsyncMock(return_value={"trained": False})
    monkeypatch.setattr(ar, "learned_retrain", spy)
    _run(ar.retrain_learned_meta(account_id="A1", user={"id": "u9"}))
    spy.assert_awaited_once_with("u9", "A1")
    spy.reset_mock()
    _run(ar.retrain_learned_meta(account_id=None, user={"id": "u9"}))
    spy.assert_awaited_once_with("u9", None)


def test_drift_check_now_retrains_scoped(monkeypatch):
    import drift_detector as dd
    import routes.analytics_routes as ar
    db = DB()
    monkeypatch.setattr("database.get_db", lambda: db)
    monkeypatch.setattr("entitlements.enforce_feature", AsyncMock())
    monkeypatch.setattr(dd, "DRIFT_ENABLED", True)
    chk = AsyncMock(return_value={"sessions": {"NY": {"drift_detected": True}}})
    monkeypatch.setattr(dd, "check_drift", chk)
    spy = AsyncMock(return_value={"trained": True})
    monkeypatch.setattr("learned_meta.retrain", spy)
    out = _run(ar.check_drift_now(account_id="A1", user={"id": "u9"}))
    assert out["retrained"] is True
    spy.assert_awaited_once_with("u9", "A1")
    assert chk.await_args.kwargs == {"user_id": "u9", "account_id": "A1"}
    assert db.drift_detector_state.docs[0]["key"] == "last_retrain:u9:A1"


def test_drift_global_retrain_also_retrains_scoped_owners(monkeypatch):
    import drift_detector as dd
    db = DB()
    db.learned_meta_residuals.docs = [
        {"user_id": "u1", "account_id": "A1", "closed_at": datetime.now(timezone.utc)},
        {"session": "NY"}]
    monkeypatch.setattr(dd, "DRIFT_ENABLED", True)
    monkeypatch.setattr(dd, "check_drift", AsyncMock(
        return_value={"sessions": {"NY": {"drift_detected": True}}}))
    spy = AsyncMock(return_value={"trained": True})
    monkeypatch.setattr("learned_meta.retrain", spy)
    out = _run(dd.maybe_trigger_retrain(db))
    assert out["retrained"] is True
    assert [c.args for c in spy.await_args_list] == [(), ("u1", "A1")]


def _pipeline_patches(monkeypatch):
    import learning_pipeline as lp
    monkeypatch.setattr(lp, "freeze_check", AsyncMock(
        return_value={"frozen": False, "reason": None}))
    monkeypatch.setattr(lp, "staged_ml_retrain", AsyncMock(return_value={"status": "ok"}))
    monkeypatch.setattr(lp, "_snapshot_version", AsyncMock())
    rl, bayes = MagicMock(), MagicMock()
    rl.train_policy = AsyncMock()
    bayes.train_model = AsyncMock()
    monkeypatch.setitem(sys.modules, "rl_policy", rl)
    monkeypatch.setitem(sys.modules, "bayes_decision", bayes)
    return lp


def test_learning_pipeline_runs_meta_labeling(monkeypatch):
    lp = _pipeline_patches(monkeypatch)
    monkeypatch.delenv("META_LABELING_ENABLED", raising=False)
    spy = AsyncMock(return_value={"trained": True, "n": 80, "n_events": 90})
    monkeypatch.setattr("meta_labeling.retrain", spy)
    db = DB()
    run = _run(lp.gated_retrain(db, "u1", trigger="t", account_id="A1"))
    spy.assert_awaited_once_with(db, "u1", "A1")
    assert run["stages"]["meta_labeling"]["status"] == "trained"


def test_learning_pipeline_meta_labeling_failure_and_switch(monkeypatch):
    lp = _pipeline_patches(monkeypatch)
    monkeypatch.setattr("meta_labeling.retrain",
                        AsyncMock(side_effect=RuntimeError("boom")))
    run = _run(lp.gated_retrain(DB(), "u1"))
    assert run["stages"]["meta_labeling"]["status"].startswith("failed")
    assert run["stages"]["ml_ensemble"] == {"status": "ok"}
    monkeypatch.setenv("META_LABELING_ENABLED", "false")
    spy = AsyncMock()
    monkeypatch.setattr("meta_labeling.retrain", spy)
    run = _run(lp.gated_retrain(DB(), "u1"))
    spy.assert_not_awaited()
    assert run["stages"]["meta_labeling"]["status"] == "disabled"
