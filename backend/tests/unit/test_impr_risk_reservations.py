"""Atomic exposure reservations — concurrent reservations never exceed the
cap, release is idempotent, rebuild self-heals leaked capacity.

An in-memory collection double implements the motor calls used
(find_one / find_one_and_update / update_one / insert_one / find().to_list)
with MongoDB's single-document atomicity: each operation completes without
yielding, while callers yield (`await asyncio.sleep(0)`) BEFORE the op so
concurrent reservers interleave exactly at the race point the old
count→insert code lost."""
import asyncio
import copy
from datetime import datetime, timedelta, timezone

import pytest

import execution_authority as ea

pytestmark = pytest.mark.unit


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class DuplicateKeyError(Exception):
    pass


def _get(doc, dotted):
    cur = doc
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None, False
        cur = cur[part]
    return cur, True


def _match(doc, flt):
    for k, cond in flt.items():
        val, present = _get(doc, k)
        if isinstance(cond, dict) and any(c.startswith("$") for c in cond):
            for op, arg in cond.items():
                if op == "$lt" and not (present and val < arg):
                    return False
                if op == "$lte" and not (present and val <= arg):
                    return False
                if op == "$in" and val not in arg:
                    return False
                if op == "$exists" and present != bool(arg):
                    return False
        elif val != cond:
            return False
    return True


def _apply(doc, upd):
    for k, v in (upd.get("$inc") or {}).items():
        doc[k] = doc.get(k, 0) + v
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
    def __init__(self, n):
        self.modified_count = n
        self.matched_count = n


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    async def to_list(self, length=None):
        return [copy.deepcopy(r) for r in self.rows]


class Coll:
    def __init__(self):
        self.docs = {}
        self.ops = 0

    async def find_one(self, flt, proj=None, sort=None):
        await asyncio.sleep(0)
        for d in self.docs.values():
            if _match(d, flt):
                return copy.deepcopy(d)
        return None

    async def find_one_and_update(self, flt, upd, **kw):
        await asyncio.sleep(0)          # yield BEFORE the atomic op
        self.ops += 1
        for d in self.docs.values():
            if _match(d, flt):
                _apply(d, upd)
                return copy.deepcopy(d)
        return None

    async def update_one(self, flt, upd, upsert=False):
        await asyncio.sleep(0)
        for d in self.docs.values():
            if _match(d, flt):
                _apply(d, upd)
                return _Res(1)
        return _Res(0)

    async def insert_one(self, doc):
        await asyncio.sleep(0)
        key = doc.get("_id")
        if key in self.docs:
            raise DuplicateKeyError("duplicate key")
        self.docs[key] = copy.deepcopy(doc)
        return _Res(1)

    def find(self, flt, proj=None):
        return _Cursor([d for d in self.docs.values() if _match(d, flt)])


class FakeDB:
    def __init__(self):
        self.exposure_reservations = Coll()
        self.trades = Coll()


def _trade(i, *, acct="A1", user="u1", origin="auto", status="open",
           rsv=None, lot=0.1):
    d = {"_id": f"t{i}", "account_id": acct, "user_id": user,
         "origin": origin, "status": status, "symbol": "EURUSD",
         "lot_size": lot, "entry_price": 1.10, "stop_loss": 1.09}
    if rsv:
        d["exposure_reservation"] = rsv
    return d


async def _reserve(db, **kw):
    base = dict(user_id="u1", account_id="A1", cfg_account_id="A1",
                symbol="EURUSD", lot=0.1, risk_usd=100.0, auto=True,
                max_concurrent=3, max_total=5)
    base.update(kw)
    return await ea.reserve_exposure(db, **base)


# ── concurrency ─────────────────────────────────────────────────────────
def test_concurrent_reservations_never_exceed_cap():
    db = FakeDB()

    async def go():
        return await asyncio.gather(*[_reserve(db) for _ in range(25)])
    results = _run(go())
    ok = [r for r in results if r["ok"]]
    blocked = [r for r in results if not r["ok"]]
    assert len(ok) == 3
    assert all(r["blocked"] == "max_concurrent_cap" for r in blocked)
    doc = db.exposure_reservations.docs["acct:A1"]
    assert doc["auto_slots"] == 3 and doc["total_slots"] == 3
    assert len(doc["holds"]) == 3


def test_seed_counts_existing_open_trades():
    db = FakeDB()
    for i in range(2):
        db.trades.docs[f"t{i}"] = _trade(i)
    db.trades.docs["closed"] = _trade(9, status="closed")
    r1 = _run(_reserve(db))
    r2 = _run(_reserve(db))
    assert r1["ok"] and not r2["ok"]
    assert r2["inflight"] == 3 and r2["cap"] == 3 and r2["atomic"]


def test_total_cap_counts_manual_positions():
    db = FakeDB()
    for i in range(5):
        db.trades.docs[f"m{i}"] = _trade(i, origin="manual")
    r = _run(_reserve(db))
    assert not r["ok"] and r["total_inflight"] == 5 and r["total_cap"] == 5


def test_open_risk_cap_atomic():
    db = FakeDB()

    async def go():
        return await asyncio.gather(*[
            _reserve(db, max_concurrent=0, max_total=None,
                     risk_usd=400.0, max_risk_usd=900.0)
            for _ in range(6)])
    res = _run(go())
    assert sum(r["ok"] for r in res) == 2
    assert {r["blocked"] for r in res if not r["ok"]} == {"open_risk_cap"}
    assert db.exposure_reservations.docs["acct:A1"]["risk_usd"] == \
        pytest.approx(800.0)


def test_split_scope_compensates_on_risk_failure():
    """Default (user-wide) cfg: slots on user scope, risk on account doc;
    a risk refusal must roll the slot back."""
    db = FakeDB()
    r = _run(_reserve(db, cfg_account_id=None, risk_usd=500.0,
                      max_risk_usd=100.0))
    assert not r["ok"] and r["blocked"] == "open_risk_cap"
    user_doc = db.exposure_reservations.docs["user:u1"]
    assert user_doc["total_slots"] == 0 and user_doc["holds"] == {}


# ── release ─────────────────────────────────────────────────────────────
def test_release_frees_slot_and_is_idempotent():
    db = FakeDB()
    rs = [_run(_reserve(db)) for _ in range(3)]
    assert not _run(_reserve(db))["ok"]
    trade = _trade(1, rsv=rs[0]["reservation"])
    assert _run(ea.release_reservation(db, trade)) == 1
    assert _run(ea.release_reservation(db, trade)) == 0      # idempotent
    doc = db.exposure_reservations.docs["acct:A1"]
    assert doc["auto_slots"] == 2 and doc["risk_usd"] == pytest.approx(200)
    assert _run(_reserve(db))["ok"]


def test_release_by_reservation_dict_and_legacy_trade():
    db = FakeDB()
    r = _run(_reserve(db))
    assert _run(ea.release_reservation(db, r["reservation"])) == 1
    # legacy trade (no stored reservation) — hold id trade:<_id> after rebuild
    db.trades.docs["t7"] = _trade(7)
    _run(ea.rebuild_reservations(db, "A1"))
    assert db.exposure_reservations.docs["acct:A1"]["total_slots"] == 1
    assert _run(ea.release_reservation(db, _trade(7))) == 1
    assert db.exposure_reservations.docs["acct:A1"]["total_slots"] == 0
    assert _run(ea.release_reservation(db, None)) == 0


# ── rebuild (self-healing) ──────────────────────────────────────────────
def test_rebuild_heals_missing_release():
    db = FakeDB()
    rs = [_run(_reserve(db)) for _ in range(3)]
    for i, r in enumerate(rs):
        db.trades.docs[f"t{i}"] = _trade(i, rsv=r["reservation"])
    # trade t0 closes but nobody calls release_reservation
    db.trades.docs["t0"]["status"] = "closed"
    assert not _run(_reserve(db))["ok"]           # capacity leaked
    out = _run(ea.rebuild_reservations(db, "A1"))
    assert out["rebuilt"] and out["auto_slots"] == 2
    assert out["risk_usd"] == pytest.approx(200.0)  # stored reserved risk
    assert _run(_reserve(db))["ok"]


def test_rebuild_keeps_young_unbound_holds_and_drops_old():
    db = FakeDB()
    r = _run(_reserve(db))                        # in-flight, no trade row
    out = _run(ea.rebuild_reservations(db, "A1"))
    assert out["total_slots"] == 1                 # young hold kept
    doc = db.exposure_reservations.docs["acct:A1"]
    old = (datetime.now(timezone.utc)
           - timedelta(seconds=ea.HOLD_TTL_SEC + 5)).isoformat()
    doc["holds"][r["reservation"]["id"]]["at"] = old
    out = _run(ea.rebuild_reservations(db, "A1"))
    assert out["total_slots"] == 0                 # leaked hold dropped


def test_rebuild_cas_never_clobbers_concurrent_reservation():
    db = FakeDB()
    _run(_reserve(db))
    coll = db.exposure_reservations
    real_find_one = coll.find_one

    async def racing_find_one(flt, proj=None, sort=None):
        doc = await real_find_one(flt, proj, sort)
        # a reservation lands between the rebuild's read and its write
        coll.docs["acct:A1"]["rev"] += 1
        return doc
    coll.find_one = racing_find_one
    out = _run(ea.rebuild_reservations(db, "A1"))
    assert out["rebuilt"] is False and out["reason"] == "concurrent_update"


def test_rebuild_risk_for_legacy_rows_matches_guardian_math():
    db = FakeDB()
    db.trades.docs["t1"] = _trade(1, lot=1.0)        # 100 pips × $10
    db.trades.docs["t2"] = {**_trade(2), "stop_loss": None}  # no stop
    out = _run(ea.rebuild_reservations(db, "A1",
                                       account={"equity": 10_000.0}))
    # $1000 + per-trade max (3% of 10k = $300) for the stopless row
    assert out["risk_usd"] == pytest.approx(1300.0)


# ── failure semantics ───────────────────────────────────────────────────
def test_reservation_db_error_fails_closed():
    db = FakeDB()

    async def boom(*a, **k):
        raise RuntimeError("mongo down")
    db.exposure_reservations.find_one_and_update = boom
    r = _run(_reserve(db))
    assert r == {"ok": False, "blocked": "reservation_error",
                 "reason": "RuntimeError: mongo down"}


def test_non_async_test_double_is_skipped_not_blocked():
    from unittest.mock import MagicMock
    r = _run(_reserve(MagicMock()))
    assert r["ok"] and r["reservation"] is None


# ── engine adapter (MT5 / paper path) ───────────────────────────────────
def test_engine_adapter_uses_guardian_risk_and_total_cap(monkeypatch):
    import execution
    db = FakeDB()
    safety = {"ok": True, "context": {"risk_usd": 250.0,
                                      "aggregate_risk": 250.0}}
    acct = {"_id": "A1", "equity": 10_000.0}
    sig = {"symbol": "EURUSD", "lot_size": 0.25, "entry_price": 1.1,
           "stop_loss": 1.09, "origin": "auto"}
    monkeypatch.setenv("EXEC_TOTAL_POSITIONS_BUFFER", "2")

    async def go(n):
        return await asyncio.gather(*[
            execution.reserve_trade_exposure(
                db, user_id="u1", account=acct, signal=sig, safety=safety,
                max_concurrent=10, cfg_account_id="A1") for _ in range(n)])
    res = _run(go(6))
    # 9% of $10k = $900 open-risk cap → 3 × $250 fit, the 4th does not
    assert sum(r["ok"] for r in res) == 3
    assert {r.get("blocked") for r in res if not r["ok"]} == {"open_risk_cap"}


# ── MT5 engine end-to-end: the old count→insert race is closed ──────────
def test_mt5_engine_concurrent_submits_respect_cap(monkeypatch):
    import time as _time
    import execution
    import execution_intents
    from execution_authorization import ExecutionAuthorization, _mac_for

    db = FakeDB()
    inserted = []

    async def count_documents(q):          # stale pre-filter: always 0
        await asyncio.sleep(0)
        return 0

    async def insert_one(doc):
        await asyncio.sleep(0)
        inserted.append(copy.deepcopy(doc))

        class R:
            inserted_id = f"id{len(inserted)}"
        return R()
    db.trades.count_documents = count_documents
    db.trades.insert_one = insert_one
    db.execution_intents = Coll()

    async def _none(*a, **k):
        return None

    async def _audit(**kw):
        return {"ok": True, "blocked_by": None, "audit": [],
                "context": {"risk_usd": 50.0, "aggregate_risk": 50.0}}

    async def _auth(*a, **k):
        return {"ok": True, "nonce": "n"}

    async def _bcast(*a, **k):
        return None
    monkeypatch.setattr(execution, "get_db", lambda: db)
    monkeypatch.setattr(execution, "audit_pre_trade", _audit)
    monkeypatch.setattr(execution, "authorize_order", _auth)

    async def _quote(sym):
        return {"price": 1.10}
    monkeypatch.setattr(execution, "get_quote", _quote)
    monkeypatch.setattr(execution.ws_manager, "broadcast", _bcast)
    monkeypatch.setattr("entitlements.verify_execution_entitlement", _none)
    monkeypatch.setattr("vps_agent.verify_execution_identity", _none)
    monkeypatch.setattr("microstructure.is_market_closed", lambda s: None)
    monkeypatch.setattr(execution_intents, "transition", _none)
    monkeypatch.setattr("trade_explainer.snapshot_for_trade_creation", _none)

    def _token(iid):
        ts = int(_time.time() * 1000)
        return ExecutionAuthorization(intent_id=iid, minted_at_ms=ts,
                                      nonce=f"n{iid}",
                                      mac=_mac_for(iid, ts, f"n{iid}"))

    acct = {"_id": "A1", "broker": "MT5", "mode": "live",
            "equity": 10_000.0, "balance": 10_000.0}
    sig = {"symbol": "EURUSD", "action": "BUY", "lot_size": 0.05,
           "entry_price": 1.10, "stop_loss": 1.09, "take_profit": 1.12,
           "origin": "auto", "scope": "swing"}
    eng = execution.MT5BridgeEngine()

    async def go():
        return await asyncio.gather(*[
            eng.execute_authorized(
                user_id="u1", account=dict(acct), signal=dict(sig),
                max_concurrent=1, cfg_account_id="A1",
                intent={"intent_id": f"i{k}"}, authorization=_token(f"i{k}"))
            for k in range(4)])
    res = _run(go())
    assert len(inserted) == 1
    assert sum(1 for r in res if r.get("blocked") == "max_concurrent_cap") == 3
    rsv = inserted[0]["exposure_reservation"]
    assert rsv["risk_usd"] == 50.0 and rsv["keys"] == ["acct:A1"]
    # closing the trade releases the slot for the next submit
    assert _run(ea.release_reservation(db, inserted[0])) == 1
    assert db.exposure_reservations.docs["acct:A1"]["total_slots"] == 0


# ── authority REDUCED never increases size (coordinator fix) ────────────
class _StubEngine:
    def __init__(self):
        self.signals = []

    async def execute_authorized(self, *, signal, **kw):
        self.signals.append(dict(signal))
        return {"id": "t1"}


def _authority_env(monkeypatch, reduce_factor=0.5):
    import database
    import execution_intents
    import trading_authority
    from modules.pamm import strategy_guard
    db = FakeDB()
    db.execution_intents = Coll()
    db.authority_snapshots = Coll()
    db.market_snapshots = Coll()
    db.price_ticks = Coll()
    db.pamm_programs = Coll()
    finalized = []

    async def _create(db_, **kw):
        await db.execution_intents.insert_one(
            {"_id": "i1", "intent_id": "i1", "status": "created"})
        return {"intent_id": "i1", "duplicate": False}

    async def _transition(db_, iid, to, **kw):
        finalized.append(to)

    async def _gate(db_, account=None):
        return {"ok": True, "level": "REDUCED", "reasons": ["test"],
                "reduce_factor": reduce_factor}

    async def _no_program(*a, **k):
        return None
    monkeypatch.setattr(database, "get_db", lambda: db)
    monkeypatch.setattr(execution_intents, "create_intent", _create)
    monkeypatch.setattr(execution_intents, "transition", _transition)
    monkeypatch.setattr(trading_authority, "enforce_new_trade", _gate)
    monkeypatch.setattr(strategy_guard, "resolve_program", _no_program)
    return db, finalized


_ACCT = {"_id": "A1", "trading_enabled": True}


def test_reduced_lot_is_floored_never_raised(monkeypatch):
    _authority_env(monkeypatch)
    eng = _StubEngine()
    sig = {"symbol": "EURUSD", "action": "BUY", "lot_size": 0.25,
           "entry_price": 1.1, "stop_loss": 1.09}
    _run(ea.submit_intent(user_id="u1", account=_ACCT, signal=sig,
                          engine=eng))
    assert eng.signals[0]["lot_size"] == pytest.approx(0.12)


def test_reduced_below_min_lot_blocks(monkeypatch):
    _, finalized = _authority_env(monkeypatch)
    eng = _StubEngine()
    sig = {"symbol": "EURUSD", "action": "BUY", "lot_size": 0.01,
           "entry_price": 1.1, "stop_loss": 1.09}
    out = _run(ea.submit_intent(user_id="u1", account=_ACCT, signal=sig,
                                engine=eng))
    assert out["blocked"] == "trading_authority_reduce_below_min_lot"
    assert eng.signals == [] and "cancelled" in finalized


def test_reduced_crypto_amount_halved_without_lot_floor(monkeypatch):
    _authority_env(monkeypatch)
    eng = _StubEngine()
    sig = {"symbol": "BTCUSDT", "action": "BUY", "lot_size": 0.001,
           "entry_price": 60000, "stop_loss": 59000,
           "_crypto_requested_amount": 0.001}
    _run(ea.submit_intent(user_id="u1",
                          account={**_ACCT, "kind": "binance"},
                          signal=sig, engine=eng))
    assert eng.signals[0]["lot_size"] == pytest.approx(0.0005)


def test_settle_paper_query_excludes_crypto_testnet():
    import inspect
    import execution
    src = inspect.getsource(execution.settle_paper_trades_against_price)
    assert '"broker_kind": {"$ne": "binance"}' in src
