"""Audit v2 (ops) — vault rewrap readiness must fail closed on missing worker
acknowledgements; the first-time lease insert must write the ack (pure, mocks)."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.unit
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

KEY = "abc123def456"


class _Cursor:
    def __init__(self, docs):
        self.docs = docs

    async def to_list(self, n):
        return list(self.docs)[:n]


class _Coll:
    def __init__(self, docs=None):
        self.docs = list(docs or [])
        self.inserted = []
        self.updates = []

    async def find_one(self, *a, **k):
        return self.docs[0] if self.docs else None

    def find(self, *a, **k):
        return _Cursor(self.docs)


class _DB:
    def __init__(self, manifest, leases):
        self.secrets_rewrap_manifests = _Coll([manifest] if manifest else [])
        self.worker_leases = _Coll(leases)


def _manifest(status="complete"):
    return {"_id": "rewrap_x", "status": status, "to_key_id": KEY, "failed": []}


def _lease(name, key=KEY, live=True, **extra):
    now = datetime.now(timezone.utc)
    exp = now + timedelta(seconds=30) if live else now - timedelta(minutes=5)
    d = {"_id": name, "holder": f"h:{name}", "expires_at": exp}
    if key is not ...:
        d["vault_key_id"] = key
    d.update(extra)
    return d


def _rr(db):
    import integrations_settings as integ
    return asyncio.run(integ.rewrap_readiness(db))


def test_no_manifest_is_ok():
    assert _rr(_DB(None, []))["ok"] is True


def test_missing_ack_is_not_ok():
    r = _rr(_DB(_manifest(), [_lease("trading"), _lease("protection", key=...), _lease("model", key=None)]))
    assert r["ok"] is False
    assert set(r["unacknowledged"]) == {"protection", "model"}
    assert set(r["workers_missing_ack"]) == {"protection", "model"}
    assert r["workers_on_old_key"] == []


def test_old_key_is_unacknowledged():
    r = _rr(_DB(_manifest(), [_lease("trading", key="oldkey000000")]))
    assert r["ok"] is False and r["workers_on_old_key"] == ["trading"]
    assert r["unacknowledged"] == ["trading"]


def test_zero_live_leases_with_manifest_is_not_ok():
    r = _rr(_DB(_manifest(), []))
    assert r["ok"] is False and "no live workers have acknowledged" in r["detail"]


def test_expired_lease_is_ignored():
    # an expired lease on the OLD key does not block; a live acked lease passes
    r = _rr(_DB(_manifest(), [_lease("trading"), _lease("model", key="oldkey000000", live=False),
                              _lease("legacy", key=None, expires_at="2020-01-01T00:00:00")]))
    assert r["ok"] is True and r["live_workers"] == ["trading"]
    # only expired leases → treated as zero live workers → not ok
    r = _rr(_DB(_manifest(), [_lease("trading", live=False)]))
    assert r["ok"] is False and "no live workers have acknowledged" in r["detail"]


def test_all_acked_is_ok_and_shape_kept():
    r = _rr(_DB(_manifest(), [_lease("trading"), _lease("inprocess-ops"),
                              _lease("analytics", expires_at=datetime.now(timezone.utc).replace(tzinfo=None)
                                     + timedelta(seconds=30))]))   # naive BSON datetime = UTC
    assert r["ok"] is True
    for k in ("ok", "manifest_id", "status", "failed", "workers_on_old_key", "detail", "unacknowledged"):
        assert k in r
    assert r["unacknowledged"] == []


def test_incomplete_manifest_is_not_ok_even_if_acked():
    assert _rr(_DB(_manifest("incomplete"), [_lease("trading")]))["ok"] is False


def test_first_time_lease_insert_writes_vault_ack(monkeypatch):
    from workers import base

    class _Res:
        matched_count = 0

    class _Leases:
        def __init__(self):
            self.inserted = []

        async def update_one(self, *a, **k):
            return _Res()

        async def insert_one(self, doc):
            self.inserted.append(doc)

    class _Db:
        worker_leases = _Leases()

    monkeypatch.setattr(base, "_vault_key_id", lambda: KEY)
    db = _Db()
    assert asyncio.run(base._try_acquire(db, "trading")) is True
    doc = db.worker_leases.inserted[0]
    assert doc["_id"] == "trading" and doc["vault_key_id"] == KEY
    # and that freshly inserted lease satisfies readiness
    r = _rr(_DB(_manifest(), [doc]))
    assert r["ok"] is True
