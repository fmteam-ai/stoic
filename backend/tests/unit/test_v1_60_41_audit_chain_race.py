"""v1.60.41 — audit_chain.append_chained races (15 Oct 11:46:58: 8 appends in ~120 ms → duplicate seq 77/78
with the same prev_hash → CRITICAL I3) and the safe re-anchor for the existing break."""
import asyncio
import os
import sys
from unittest.mock import patch

import pytest
from bson import ObjectId
from pymongo.errors import DuplicateKeyError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import audit_chain as ac  # noqa: E402

pytestmark = pytest.mark.unit


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def _gather(*coros):
    return await asyncio.gather(*coros)


class _Cursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, key, direction=None):
        keys = key if isinstance(key, list) else [(key, direction or 1)]
        for k, d in reversed(keys):
            self._docs.sort(key=lambda x: (x.get(k) is None, x.get(k)), reverse=(d == -1))
        return self

    def __aiter__(self):
        self._it = iter(self._docs)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class _Res:
    def __init__(self, n):
        self.modified_count = n


class Col:
    """Motor-shaped fake: find_one YIELDS to the loop (so concurrent appenders read the same head, like
    Mongo round-trips) and insert_one enforces the unique partial seq index when `unique` is on."""

    def __init__(self, unique=True, yield_in_find=True):
        self.docs, self.unique, self.yield_in_find, self.indexes = [], unique, yield_in_find, {}

    def _match(self, d, q):
        for k, v in q.items():
            if isinstance(v, dict) and "$exists" in v:
                if (k in d) != v["$exists"]:
                    return False
            elif isinstance(v, dict) and "$gte" in v:
                if d.get(k) is None or d.get(k) < v["$gte"]:
                    return False
            elif d.get(k) != v:
                return False
        return True

    async def find_one(self, q, sort=None):
        docs = [d for d in self.docs if self._match(d, q)]           # snapshot BEFORE the round-trip yield
        if sort:
            docs = _Cursor(docs).sort(sort)._docs
        if self.yield_in_find:
            await asyncio.sleep(0)
        return dict(docs[0]) if docs else None

    def find(self, q):
        return _Cursor(dict(d) for d in self.docs if self._match(d, q))

    async def insert_one(self, doc):
        if self.unique and "entry_hash" in doc and any(d.get("seq") == doc["seq"] and "entry_hash" in d for d in self.docs):
            raise DuplicateKeyError("E11000 duplicate key error index: uniq_chain_seq")
        doc.setdefault("_id", ObjectId())
        self.docs.append(dict(doc))

    async def update_one(self, q, u):
        for d in self.docs:
            if self._match(d, q):
                d.update(u.get("$set", {}))
                return _Res(1)
        return _Res(0)

    async def update_many(self, q, u):
        n = 0
        for d in self.docs:
            if self._match(d, q):
                d.update(u.get("$set", {}))
                n += 1
        return _Res(n)

    async def count_documents(self, q):
        return sum(1 for d in self.docs if self._match(d, q))

    async def create_index(self, key, **kw):
        if kw.get("unique") and "entry_hash" in str(kw.get("partialFilterExpression")):
            seqs = [d["seq"] for d in self.docs if "entry_hash" in d]
            if len(seqs) != len(set(seqs)):
                raise DuplicateKeyError("E11000 cannot build unique index: duplicate seq")
        self.indexes[kw.get("name") or f"{key}_1"] = kw
        return kw.get("name")


class Db:
    def __init__(self, **cols):
        self.cols = cols

    def __getitem__(self, name):
        return self.cols.setdefault(name, Col())

    def __getattr__(self, name):
        if name.startswith("_") or name == "cols":
            raise AttributeError(name)
        return self[name]


@pytest.fixture(autouse=True)
def _fresh_module_state():
    ac._INDEX_READY.clear()
    ac._LOCKS.clear()
    yield
    ac._INDEX_READY.clear()
    ac._LOCKS.clear()


def _entry(i):
    return {"actor_email": "admin@x", "action": f"a{i}", "target_kind": "t", "target_id": str(i), "at": f"2026-10-15T11:46:58.{i:03d}"}


# ── the race, fixed ──────────────────────────────────────────────────────────────────────────────────────
def test_eight_concurrent_appends_in_one_process_produce_a_contiguous_chain():
    db = Db(admin_audit_log=Col())
    _run(_gather(*[ac.append_chained(db, _entry(i)) for i in range(8)]))
    seqs = sorted(d["seq"] for d in db["admin_audit_log"].docs)
    assert seqs == list(range(1, 9))
    res = _run(ac.verify_chain(db))
    assert res["ok"] and res["chained_entries"] == 8 and res["first_bad_seq"] is None
    assert "uniq_chain_seq" in db["admin_audit_log"].indexes


def test_cross_process_race_is_caught_by_the_unique_index_and_retried():
    """Two 'processes' (no shared lock) read the same head: the unique index rejects the second insert and
    the retry re-reads the head — no duplicate seq, no shared prev_hash."""
    db = Db(admin_audit_log=Col())
    col = db["admin_audit_log"]

    async def _proc(tag, n):
        ac._LOCKS.clear()                                            # each 'process' has its own lock
        for i in range(n):
            await ac.append_chained(db, dict(_entry(i), proc=tag))

    _run(_gather(_proc("api", 4), _proc("worker", 4)))
    seqs = sorted(d["seq"] for d in col.docs)
    assert seqs == list(range(1, 9))
    prevs = [d["prev_hash"] for d in col.docs]
    assert len(set(prevs)) == 8                                      # 15 Oct shape (same prev_hash twice) cannot recur
    assert _run(ac.verify_chain(db))["ok"]


def test_without_the_unique_index_the_old_algorithm_duplicated_seq():
    """Negative control — the 15 Oct behaviour: no unique index + concurrent read-modify-write → duplicate seq."""
    db = Db(admin_audit_log=Col(unique=False))

    async def _old_append(entry):
        col = db["admin_audit_log"]
        last = await col.find_one({"entry_hash": {"$exists": True}}, sort=[("seq", -1)])
        prev = last["entry_hash"] if last else ac.GENESIS
        doc = dict(entry, seq=(last["seq"] + 1) if last else 1, prev_hash=prev)
        doc["entry_hash"] = ac._entry_hash(prev, doc)
        await col.insert_one(doc)

    _run(_gather(*[_old_append(_entry(i)) for i in range(8)]))
    seqs = [d["seq"] for d in db["admin_audit_log"].docs]
    assert len(set(seqs)) < 8
    res = _run(ac.verify_chain(db))
    assert not res["ok"] and res["first_bad_seq"] is not None and res["anomalies"][0]["error"] == "chain_link_broken"


def test_append_gives_up_loudly_instead_of_writing_a_duplicate():
    class AlwaysDup(Col):
        async def insert_one(self, doc):
            raise DuplicateKeyError("E11000")
    db = Db(admin_audit_log=AlwaysDup())
    with patch.object(ac, "_APPEND_RETRIES", 3), patch("audit_chain.asyncio.sleep", return_value=None) as sl:
        with pytest.raises(RuntimeError, match="lost the seq race 3 times"):
            _run(ac.append_chained(db, _entry(0)))
    assert db["admin_audit_log"].docs == []


def test_fake_db_without_create_index_still_appends():
    class Bare(Col):
        def __getattribute__(self, name):
            if name == "create_index":
                raise AttributeError(name)
            return super().__getattribute__(name)
    db = Db(admin_audit_log=Bare())
    doc = _run(ac.append_chained(db, _entry(0)))
    assert doc["seq"] == 1 and doc["prev_hash"] == ac.GENESIS


def test_seed_builds_the_index_and_i2_requires_it():
    seed = open(os.path.join(os.path.dirname(ac.__file__), "seed.py"), encoding="utf-8").read()
    assert "ensure_seq_index(db)" in seed
    from security_agent.checks.integrity_platform_trading import REQUIRED_INDEXES
    assert REQUIRED_INDEXES["admin_audit_log"] == "uniq_chain_seq"


# ── the existing break: safe re-anchor ───────────────────────────────────────────────────────────────────
def _incident_chain(n_ok=76):
    """Reproduce 15 Oct: a clean chain to seq 76, then two seq-77 entries with the same prev_hash, two seq-78
    forks, then the chain continuing from one fork."""
    db = Db(admin_audit_log=Col(unique=False), audit_anchors=Col(unique=False))
    col = db["admin_audit_log"]
    prev = ac.GENESIS
    for i in range(1, n_ok + 1):
        d = dict(_entry(i), seq=i, prev_hash=prev)
        d["entry_hash"] = ac._entry_hash(prev, d)
        _run(col.insert_one(d))
        prev = d["entry_hash"]
    h76 = prev
    forks = []
    for tag in ("a", "b"):
        d = dict(_entry(77), seq=77, prev_hash=h76, fork=tag)
        d["entry_hash"] = ac._entry_hash(h76, d)
        _run(col.insert_one(d))
        e = dict(_entry(78), seq=78, prev_hash=d["entry_hash"], fork=tag)
        e["entry_hash"] = ac._entry_hash(d["entry_hash"], e)
        _run(col.insert_one(e))
        forks.append(e["entry_hash"])
    prev = forks[0]
    for i in range(79, 83):
        d = dict(_entry(i), seq=i, prev_hash=prev)
        d["entry_hash"] = ac._entry_hash(prev, d)
        _run(col.insert_one(d))
        prev = d["entry_hash"]
    # an anchor was taken at seq 80 before the break was noticed
    _run(db.audit_anchors.insert_one({"collection": "admin_audit_log", "seq": 80, "entry_hash": "x" * 64}))
    return db


def test_incident_chain_is_detected_with_first_bad_seq():
    db = _incident_chain()
    res = _run(ac.verify_chain(db))
    assert not res["ok"] and res["first_bad_seq"] == 77
    errs = {(a["seq"], a["error"]) for a in res["anomalies"]}
    assert (77, "chain_link_broken") in errs and (78, "chain_link_broken") in errs


def test_dry_run_plans_the_relink_and_writes_nothing():
    db = _incident_chain()
    before = [dict(d) for d in db["admin_audit_log"].docs]
    res = _run(ac.repair_chain(db, actor="ops@x", reason="15 Oct seq race", apply=False))
    assert res["ok"] and not res["applied"] and res["break_seq"] == 77
    assert res["entries_to_rewrite"] == 7                           # 77a is intact in place; 77b 78a 78b 79 80 81 82 re-linked
    assert db["admin_audit_log"].docs == before and db["audit_chain_repairs"].docs == []


def test_apply_relinks_in_insertion_order_marks_anchors_and_reanchors(monkeypatch):
    db = _incident_chain()
    originals = {d["_id"]: dict(d) for d in db["admin_audit_log"].docs}
    created = {}

    async def _anchor(_db):
        head = await _db["admin_audit_log"].find_one({"entry_hash": {"$exists": True}}, sort=[("seq", -1)])
        created.update(seq=head["seq"], entry_hash=head["entry_hash"])
        await _db.audit_anchors.insert_one({"collection": "admin_audit_log", **created})
        return dict(created)
    import audit_anchor
    monkeypatch.setattr(audit_anchor, "create_anchor", _anchor)

    res = _run(ac.repair_chain(db, actor="ops@x", reason="15 Oct seq race", apply=True))
    assert res["applied"] and res["seq_index_ok"] and res["marker_seq"] == 85 and res["anchor_seq"] == 85
    after = _run(ac.verify_chain(db))
    assert after["ok"] and after["chained_entries"] == 85 and after["first_bad_seq"] is None
    col = db["admin_audit_log"]
    by_seq = {d["seq"]: d for d in col.docs}
    assert by_seq[77]["fork"] == "a" and by_seq[78]["fork"] == "b" and by_seq[79]["fork"] == "a" and by_seq[80]["fork"] == "b"
    assert "reanchor" not in by_seq[76] and by_seq[78]["reanchor"]["orig_seq"] == 77 and by_seq[78]["reanchor"]["repair_id"] == res["repair_id"]
    assert by_seq[78]["reanchor"]["orig_entry_hash"] == originals[by_seq[78]["_id"]]["entry_hash"]
    assert by_seq[84]["reanchor"]["orig_seq"] == 82
    assert all(d["action"] == originals[d["_id"]]["action"] for d in col.docs if d["_id"] in originals)   # content untouched
    marker = by_seq[85]
    assert marker["action"] == ac.REANCHOR_ACTION and marker["detail"]["repair_id"] == res["repair_id"] and marker["detail"]["break_seq"] == 77
    rep = db["audit_chain_repairs"].docs[0]
    assert rep["repair_id"] == res["repair_id"] and len(rep["originals"]) == 7 and rep["new_head"] == marker["entry_hash"]
    assert {o["entry_hash"] for o in rep["originals"]} == {originals[d["_id"]]["entry_hash"] for d in col.docs if d.get("reanchor")}
    old_anchor = next(a for a in db.audit_anchors.docs if a["seq"] == 80 and a["entry_hash"] == "x" * 64)
    assert old_anchor["superseded_by_repair"] == res["repair_id"] and res["anchors_superseded"] == 1
    assert created["entry_hash"] == marker["entry_hash"]
    # idempotent: a second run finds the chain intact
    again = _run(ac.repair_chain(db, actor="ops@x", reason="again", apply=True))
    assert again["ok"] and not again["applied"] and again["reason"] == "chain intact"


def test_repair_refuses_altered_content():
    db = _incident_chain()
    db["admin_audit_log"].docs[10]["action"] = "tampered"
    res = _run(ac.repair_chain(db, actor="ops@x", reason="x", apply=True))
    assert not res["ok"] and "altered" in res["reason"] and "backup" in res["reason"]
    assert db["audit_chain_repairs"].docs == []


def test_plan_relink_handles_a_chain_not_starting_at_seq_one_and_an_intact_chain():
    db = Db(admin_audit_log=Col())
    for i in range(3):
        _run(ac.append_chained(db, _entry(i)))
    entries = sorted(db["admin_audit_log"].docs, key=lambda d: d["seq"])
    assert ac.plan_relink(entries)["reason"] == "chain intact"
    assert ac.plan_relink([])["rewrites"] == []
    prev, shifted = ac.GENESIS, []                                     # purge of early entries: chain starts at seq 101
    for i in range(101, 104):
        d = dict(_entry(i), seq=i, prev_hash=prev, _id=ObjectId())
        d["entry_hash"] = ac._entry_hash(prev, {k: v for k, v in d.items() if k != "_id"})
        shifted.append(d)
        prev = d["entry_hash"]
    assert ac.plan_relink(shifted)["reason"] == "chain intact"


def test_i3_reports_the_first_bad_seq():
    src = open(os.path.join(os.path.dirname(ac.__file__), "security_agent", "checks", "integrity_platform_trading.py"), encoding="utf-8").read()
    assert "first_bad_seq" in src


def test_operator_script_exists_and_is_dry_run_by_default():
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), "scripts", "reanchor_audit_chain.py")
    src = open(p, encoding="utf-8").read()
    assert 'ap.add_argument("--apply", action="store_true")' in src and "repair_chain(db, actor=args.actor, reason=args.reason, apply=args.apply)" in src
