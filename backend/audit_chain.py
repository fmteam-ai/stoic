"""Tamper-evident hash-chained audit log (iter-136).

Each chained entry stores: seq (monotonic), prev_hash (previous entry_hash
or GENESIS) and entry_hash = sha256(prev_hash + canonical(entry)). Any
mutation, deletion or reorder breaks the chain and is detected by
verify_chain(). Legacy (pre-chain) entries are counted but not verified.

v1.60.41 — appends are serialised: a unique partial index on seq (chained entries only) makes a lost
read-modify-write race surface as DuplicateKeyError, which is retried from a fresh head; an in-process
lock keeps same-process writers from racing at all. 15 Oct 11:46:58: 8 appends within ~120 ms produced
duplicate seq 77/78 with the same prev_hash → CRITICAL I3. repair_chain() is the safe re-anchor for that.
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import logging
import random
import uuid
from datetime import datetime, timezone

from pymongo.errors import DuplicateKeyError

logger = logging.getLogger(__name__)

GENESIS = "GENESIS"
SEQ_INDEX = "uniq_chain_seq"
REPAIRS_COLLECTION = "audit_chain_repairs"
REANCHOR_ACTION = "audit_chain_reanchored"
_APPEND_RETRIES = 12
_INDEX_READY: set = set()
_LOCKS: dict = {}


def _canon(d: dict) -> str:
    return json.dumps(d, sort_keys=True, separators=(",", ":"), default=str)


def _entry_hash(prev: str, body: dict) -> str:
    return hashlib.sha256((prev + _canon(body)).encode()).hexdigest()


def _lock(collection: str) -> asyncio.Lock:
    lk = _LOCKS.get(collection)
    if lk is None:
        lk = _LOCKS[collection] = asyncio.Lock()
    return lk


async def ensure_seq_index(db, collection: str = "admin_audit_log") -> bool:
    """Unique partial index on seq for chained entries. False (and a warning) when it cannot be built —
    i.e. duplicates already exist: run repair_chain() first."""
    if collection in _INDEX_READY:
        return True
    try:
        await db[collection].create_index("seq", unique=True, name=SEQ_INDEX,
                                          partialFilterExpression={"entry_hash": {"$exists": True}})
        _INDEX_READY.add(collection)
        return True
    except Exception as e:  # noqa: BLE001 — fakes without create_index, or duplicates already present
        logger.warning("audit chain: unique seq index on %s not available (%s)", collection, e)
        return False


async def append_chained(db, entry: dict,
                         collection: str = "admin_audit_log") -> dict:
    col = db[collection]
    await ensure_seq_index(db, collection)
    async with _lock(collection):
        for attempt in range(_APPEND_RETRIES):
            last = await col.find_one({"entry_hash": {"$exists": True}},
                                      sort=[("seq", -1)])
            prev = last["entry_hash"] if last else GENESIS
            doc = dict(entry)
            doc["seq"] = (last["seq"] + 1) if last else 1
            doc["prev_hash"] = prev
            doc["entry_hash"] = _entry_hash(prev, {k: v for k, v in doc.items()
                                                   if k != "entry_hash"})
            try:
                await col.insert_one(doc)
                return doc
            except DuplicateKeyError:
                doc.pop("_id", None)                          # another process took this seq — re-read the head
                await asyncio.sleep(random.uniform(0.002, 0.02) * (attempt + 1))
    raise RuntimeError(f"audit chain append lost the seq race {_APPEND_RETRIES} times on {collection}")


async def verify_chain(db, collection: str = "admin_audit_log") -> dict:
    col = db[collection]
    prev = GENESIS
    checked = 0
    anomalies = []
    async for e in col.find({"entry_hash": {"$exists": True}}).sort("seq", 1):
        checked += 1
        body = {k: v for k, v in e.items()
                if k not in ("_id", "entry_hash")}
        if e.get("prev_hash") != prev:
            anomalies.append({"seq": e.get("seq"),
                              "error": "chain_link_broken"})
        if _entry_hash(e.get("prev_hash", ""), body) != e.get("entry_hash"):
            anomalies.append({"seq": e.get("seq"),
                              "error": "entry_hash_mismatch"})
        prev = e.get("entry_hash")
        if len(anomalies) >= 20:
            break
    legacy = await col.count_documents({"entry_hash": {"$exists": False}})
    return {"ok": not anomalies, "chained_entries": checked,
            "legacy_unchained_entries": legacy, "anomalies": anomalies,
            "first_bad_seq": anomalies[0]["seq"] if anomalies else None}


# ── safe re-anchor ───────────────────────────────────────────────────────────────────────────────────────
def _own_hash_ok(e: dict) -> bool:
    body = {k: v for k, v in e.items() if k not in ("_id", "entry_hash")}
    return _entry_hash(e.get("prev_hash", ""), body) == e.get("entry_hash")


def plan_relink(entries: list, repair_id: str | None = None) -> dict:
    """Pure. `entries` = chained docs in (seq, _id) order. Every entry must still verify on its OWN hash
    (content intact); only the links/sequence are rewritten from the first break onward, in that
    (seq, insertion) order. Returns {"ok", "reason", "break_index", "rewrites": [{"_id", "seq", "prev_hash", "entry_hash",
    "reanchor"}], "old_head", "new_head"} — nothing is written here."""
    altered = [e.get("seq") for e in entries if not _own_hash_ok(e)]
    if altered:
        return {"ok": False, "reason": f"entry content altered at seq {altered[:5]} — a re-anchor would legitimise "
                                       "tampering; restore from backup instead", "rewrites": []}
    prev = GENESIS
    break_index = None
    for i, e in enumerate(entries):
        if e.get("prev_hash") != prev or (i and e.get("seq") != entries[i - 1].get("seq") + 1):
            break_index = i
            break
        prev = e["entry_hash"]
    if break_index is None:
        return {"ok": True, "reason": "chain intact", "break_index": None, "rewrites": [],
                "old_head": entries[-1]["entry_hash"] if entries else GENESIS,
                "new_head": entries[-1]["entry_hash"] if entries else GENESIS}
    rewrites = []
    seq = entries[break_index - 1]["seq"] if break_index else 0
    for e in entries[break_index:]:
        seq += 1
        body = {k: v for k, v in e.items() if k not in ("_id", "entry_hash")}
        body["seq"] = seq
        body["prev_hash"] = prev
        body["reanchor"] = {"orig_seq": e.get("seq"), "orig_prev_hash": e.get("prev_hash"),
                            "orig_entry_hash": e.get("entry_hash"), "repair_id": repair_id}
        h = _entry_hash(prev, body)
        rewrites.append({"_id": e["_id"], "seq": seq, "prev_hash": prev, "entry_hash": h, "reanchor": body["reanchor"]})
        prev = h
    return {"ok": True, "reason": f"link break at seq {entries[break_index].get('seq')} (position {break_index + 1})",
            "break_index": break_index, "break_seq": entries[break_index].get("seq"), "rewrites": rewrites,
            "old_head": entries[-1]["entry_hash"], "new_head": prev}


async def repair_chain(db, *, actor: str, reason: str, apply: bool = False,
                       collection: str = "admin_audit_log") -> dict:
    """Safe re-anchor of a chain whose LINKS broke (duplicate seq / fork) while every entry's content is
    intact. Dry-run by default. apply=True: snapshot the originals into audit_chain_repairs, rewrite
    seq/prev_hash/entry_hash from the break onward (each rewritten entry carries `reanchor` with its
    original seq/hashes, covered by its new hash), append a chained `audit_chain_reanchored` entry naming
    the repair, build the unique seq index and anchor the new head. Refuses altered content."""
    col = db[collection]
    entries = [e async for e in col.find({"entry_hash": {"$exists": True}}).sort([("seq", 1), ("_id", 1)])]
    repair_id = uuid.uuid4().hex[:12]
    plan = plan_relink(entries, repair_id)
    out = {"ok": plan["ok"], "reason": plan["reason"], "applied": False, "break_seq": plan.get("break_seq"),
           "entries_to_rewrite": len(plan["rewrites"]), "old_head": plan.get("old_head"), "new_head": plan.get("new_head"),
           "chained_entries": len(entries)}
    if not plan["ok"] or not plan["rewrites"] or not apply:
        return out
    by_id = {e["_id"]: e for e in entries}
    now = datetime.now(timezone.utc).isoformat()
    await db[REPAIRS_COLLECTION].insert_one({
        "repair_id": repair_id, "at": now, "actor": actor, "reason": reason, "collection": collection,
        "break_seq": plan["break_seq"], "old_head": plan["old_head"], "new_head_before_marker": plan["new_head"],
        "originals": [{k: v for k, v in by_id[r["_id"]].items()} for r in plan["rewrites"]],
    })
    for r in plan["rewrites"]:
        await col.update_one({"_id": r["_id"]}, {"$set": {"seq": r["seq"], "prev_hash": r["prev_hash"],
                                                          "entry_hash": r["entry_hash"],
                                                          "reanchor": r["reanchor"]}})
    # the repair itself is on the chain and covered by the new head
    marker = await append_chained(db, {"actor_email": actor, "action": REANCHOR_ACTION, "target_kind": "platform",
                                       "target_id": collection, "at": now,
                                       "detail": {"repair_id": repair_id, "reason": reason, "break_seq": plan["break_seq"],
                                                  "entries_rewritten": len(plan["rewrites"]),
                                                  "old_head": plan["old_head"]}}, collection=collection)
    superseded = await db.audit_anchors.update_many(
        {"collection": collection, "seq": {"$gte": plan["break_seq"]}, "superseded_by_repair": {"$exists": False}},
        {"$set": {"superseded_by_repair": repair_id}})
    index_ok = await ensure_seq_index(db, collection)
    anchor = None
    try:
        from audit_anchor import create_anchor
        anchor = await create_anchor(db)
    except Exception as e:  # noqa: BLE001
        logger.warning("re-anchor: new anchor not created (%s) — run POST /api/ops/audit-anchor", e)
    await db[REPAIRS_COLLECTION].update_one({"repair_id": repair_id}, {"$set": {
        "marker_seq": marker["seq"], "new_head": marker["entry_hash"], "seq_index_ok": index_ok,
        "anchors_superseded": getattr(superseded, "modified_count", None), "anchor_seq": (anchor or {}).get("seq")}})
    out.update({"applied": True, "repair_id": repair_id, "marker_seq": marker["seq"], "new_head": marker["entry_hash"],
                "seq_index_ok": index_ok, "anchor_seq": (anchor or {}).get("seq"),
                "anchors_superseded": getattr(superseded, "modified_count", None)})
    return out
