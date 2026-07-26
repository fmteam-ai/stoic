"""Tamper-evident hash-chained audit log (iter-136).

Each chained entry stores: seq (monotonic), prev_hash (previous entry_hash
or GENESIS) and entry_hash = sha256(prev_hash + canonical(entry)). Any
mutation, deletion or reorder breaks the chain and is detected by
verify_chain(). Legacy (pre-chain) entries are counted but not verified.
"""
import hashlib
import json

GENESIS = "GENESIS"


def _canon(d: dict) -> str:
    return json.dumps(d, sort_keys=True, separators=(",", ":"), default=str)


def _entry_hash(prev: str, body: dict) -> str:
    return hashlib.sha256((prev + _canon(body)).encode()).hexdigest()


async def append_chained(db, entry: dict,
                         collection: str = "admin_audit_log") -> dict:
    col = db[collection]
    last = await col.find_one({"entry_hash": {"$exists": True}},
                              sort=[("seq", -1)])
    prev = last["entry_hash"] if last else GENESIS
    doc = dict(entry)
    doc["seq"] = (last["seq"] + 1) if last else 1
    doc["prev_hash"] = prev
    doc["entry_hash"] = _entry_hash(prev, {k: v for k, v in doc.items()
                                           if k != "entry_hash"})
    await col.insert_one(doc)
    return doc


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
            "legacy_unchained_entries": legacy, "anomalies": anomalies}
