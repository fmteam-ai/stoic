"""Audit v2 (ops) — verified-performance freshness keys on reconciliation /
heartbeat / statement coverage, NEVER on last trade time (pure, mocks)."""
import asyncio
import os
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

pytestmark = pytest.mark.unit
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

NOW = datetime.now(timezone.utc)
UID = "u-quiet"


def _match(doc, flt):
    for k, v in (flt or {}).items():
        if isinstance(v, dict) and "$ne" in v:
            if doc.get(k) == v["$ne"]:
                return False
        elif isinstance(v, dict):
            continue
        elif doc.get(k) != v:
            return False
    return True


class _Cursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, key, direction=1):
        self.docs = sorted(self.docs, key=lambda d: d.get(key) or 0, reverse=direction == -1)
        return self

    def limit(self, n):
        self.docs = self.docs[:n]
        return self

    async def to_list(self, n):
        return self.docs[:n]

    def __aiter__(self):
        async def gen():
            for d in self.docs:
                yield d
        return gen()


class _Coll:
    def __init__(self, docs=()):
        self.docs = list(docs)

    def find(self, flt=None, proj=None):
        return _Cursor([d for d in self.docs if _match(d, flt)])

    async def find_one(self, flt=None, proj=None, sort=None):
        docs = [d for d in self.docs if _match(d, flt)]
        if sort:
            k, direction = sort[0]
            docs.sort(key=lambda d: d.get(k) or 0, reverse=direction == -1)
        return docs[0] if docs else None

    async def count_documents(self, flt):
        return 0


class _DB:
    def __init__(self, accounts, deals, ledger):
        self.accounts = _Coll(accounts)
        self.broker_deals = _Coll(deals)
        self.reconciliation_ledger = _Coll(ledger)
        self.trades = _Coll()


def _world(*, recon_age=timedelta(minutes=2), hb_age=timedelta(seconds=10),
           last_trade_age=timedelta(days=9), coverage_to_age=timedelta(days=1), ledger=True):
    aid = ObjectId()
    acc = {"_id": aid, "user_id": UID, "label": "Quiet", "mode": "live", "trading_enabled": True,
           "status": "connected", "broker_environment": "LIVE",
           "last_heartbeat": (NOW - hb_age).isoformat(), "reconciliation_seq": 4}
    if recon_age is not None:
        acc["last_reconciled_at"] = (NOW - recon_age).isoformat()
    deals = [{"user_id": UID, "account_id": str(aid), "deal_entry": "out", "profit": 25.0,
              "deal_time": int((NOW - last_trade_age).timestamp())}]
    rows = [{"user_id": UID, "account_id": str(aid), "statement_id": "ST-1", "status": "RECONCILED",
             "period_from": (NOW - timedelta(days=40)).isoformat(),
             "period_to": (NOW - coverage_to_age).isoformat()}] if ledger else []
    return _DB([acc], deals, rows), acc


def _payload(db):
    from routes.performance_routes import _verified_payload
    return asyncio.run(_verified_payload(db, UID))


def test_quiet_reconciled_account_is_fresh_and_shareable():
    db, _ = _world(last_trade_age=timedelta(days=9))   # no trade for 9 days
    out = _payload(db)
    p = out["provenance"]
    assert p["stale"] is False and p["share_allowed"] is True and out["share_allowed"] is True
    assert p["freshness_s"] is not None and p["freshness_s"] < 3600
    assert p["statement_coverage"]["stale"] is False and p["statement_coverage"]["coverage_end"]


def test_last_trade_at_exposed_separately_from_as_of():
    db, _ = _world(last_trade_age=timedelta(days=9))
    out = _payload(db)
    lt = datetime.fromisoformat(out["last_trade_at"])
    assert abs((NOW - lt) - timedelta(days=9)) < timedelta(seconds=5)
    assert out["provenance"]["last_trade_at"] == out["last_trade_at"]
    assert out["provenance"]["as_of"] != out["last_trade_at"]
    assert datetime.fromisoformat(out["provenance"]["as_of"]) > NOW - timedelta(hours=1)
    assert out["provenance"]["last_point_at"] is not None


def test_stale_statement_coverage_blocks_share():
    db, _ = _world(coverage_to_age=timedelta(days=12))   # > STATEMENT_COVERAGE_FRESHNESS_H
    p = _payload(db)["provenance"]
    assert p["statement_coverage"]["stale"] is True
    assert p["stale"] is True and p["share_allowed"] is False


def test_unreconciled_account_blocked():
    db, _ = _world(recon_age=None, last_trade_age=timedelta(minutes=5))   # fresh trades do NOT rescue it
    p = _payload(db)["provenance"]
    assert p["stale"] is True and p["share_allowed"] is False and p["cache_status"] == "unreconciled"


def test_old_reconciliation_blocked():
    db, _ = _world(recon_age=timedelta(days=3), last_trade_age=timedelta(minutes=5))
    p = _payload(db)["provenance"]
    assert p["stale"] is True and p["share_allowed"] is False


def test_ledger_read_failure_fails_closed():
    db, _ = _world()

    class _Boom(_Coll):
        def find(self, *a, **k):
            raise RuntimeError("mongo down")
    db.reconciliation_ledger = _Boom()
    p = _payload(db)["provenance"]
    assert p["stale"] is True and p["share_allowed"] is False


# ───────────── attestation gate: reconciliation age, not deal age ─────────────

def _gate(monkeypatch, db, ledger_reasons=()):
    import broker_statement_ledger as bl
    import routes.performance_routes as pr

    async def _stats(user):
        return {"reconciliation": {"status": "RECONCILED"}}

    async def _contract(db_, uid):
        return {"accounts": [{"account_enabled": True, "position_truth": "FRESH"}]}

    async def _lg(db_, uid):
        return list(ledger_reasons)
    monkeypatch.setitem(sys.modules, "routes.trade_routes", types.SimpleNamespace(trade_stats=_stats))
    monkeypatch.setitem(sys.modules, "state_contract", types.SimpleNamespace(contract=_contract))
    monkeypatch.setattr(bl, "ledger_gate", _lg)
    monkeypatch.setattr(pr, "_attestation_environment", lambda a: "LIVE")
    return asyncio.run(pr._attestation_gate(db, UID))


def test_gate_quiet_reconciled_account_not_stale(monkeypatch):
    db, _ = _world(last_trade_age=timedelta(days=9))
    reasons = _gate(monkeypatch, db)
    assert "BROKER_DATA_STALE" not in reasons and "BROKER_DATA_AGE_UNKNOWN" not in reasons
    assert reasons == []


def test_gate_stale_reconciliation_blocked_even_with_fresh_trade(monkeypatch):
    db, _ = _world(recon_age=timedelta(hours=7), last_trade_age=timedelta(minutes=1))
    assert "BROKER_DATA_STALE" in _gate(monkeypatch, db)


def test_gate_missing_reconciliation_fails_closed(monkeypatch):
    db, _ = _world(recon_age=None, last_trade_age=timedelta(minutes=1))
    assert "BROKER_DATA_AGE_UNKNOWN" in _gate(monkeypatch, db)


def test_gate_keeps_statement_coverage_stale(monkeypatch):
    db, _ = _world()
    assert "STATEMENT_COVERAGE_STALE" in _gate(monkeypatch, db, ["STATEMENT_COVERAGE_STALE"])
