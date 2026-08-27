"""Unified Execution Intents (v55 §1/§2) — every execution-capable
subsystem (Scalp, Swing, AI, PAMM, Allocator, manual) mints an
ExecutionIntent BEFORE any order-affecting action. Formal invariant:
one intent_id causes AT MOST ONE logical trading action — API retries,
VPS restarts and duplicate callbacks converge on the stored result
instead of re-executing. Lifecycle: CREATED → SUBMITTED → ACKED →
FILLED / REJECTED / EXPIRED, with a full audit trail per intent."""
import hashlib
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

logger = logging.getLogger("execution.intents")

STATES = ["created", "submitted", "acked", "filled", "rejected", "expired"]
TERMINAL = {"filled", "rejected", "expired"}
_TRANSITIONS = {
    "created": {"submitted", "rejected", "expired"},
    "submitted": {"acked", "filled", "rejected", "expired"},
    "acked": {"filled", "rejected", "expired"},
}
_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford alphabet


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_intent_id() -> str:
    """ULID-style id: 48-bit ms timestamp + 80 random bits in Crockford
    base32 — lexicographically time-ordered and globally unique."""
    ts = int(time.time() * 1000)
    head = []
    for _ in range(10):
        head.append(_B32[ts & 31])
        ts >>= 5
    n = int.from_bytes(uuid.uuid4().bytes[:10], "big")
    tail = []
    for _ in range(16):
        tail.append(_B32[n & 31])
        n >>= 5
    return "xin_" + "".join(reversed(head)) + "".join(reversed(tail))


def dedupe_key_for(source: str, kind: str, *parts) -> str:
    raw = "|".join(str(p) for p in (source, kind, *parts))
    return hashlib.sha256(raw.encode()).hexdigest()[:40]


async def ensure_intent_indexes(db) -> None:
    await db.execution_intents.create_index("intent_id", unique=True)
    await db.execution_intents.create_index("dedupe_key", unique=True,
                                            sparse=True)
    await db.execution_intents.create_index([("created_at", -1)])
    await db.execution_intents.create_index([("source", 1), ("status", 1)])


async def create_intent(db, *, source: str, kind: str,
                        dedupe_key: str | None = None,
                        payload: dict | None = None,
                        program_id: str | None = None,
                        account_id: str | None = None,
                        actor: str | None = None) -> dict:
    """Insert a new intent. A duplicate dedupe_key returns the ORIGINAL
    intent flagged duplicate=True — never a second one."""
    doc = {"intent_id": new_intent_id(), "source": source, "kind": kind,
           "payload": payload or {}, "program_id": program_id,
           "account_id": account_id, "actor": actor, "status": "created",
           "result": None, "created_at": _now(),
           "history": [{"to": "created", "at": _now()}]}
    if dedupe_key:
        doc["dedupe_key"] = dedupe_key
    try:
        await db.execution_intents.insert_one(dict(doc))
        doc.pop("_id", None)
        return {**doc, "duplicate": False}
    except DuplicateKeyError:
        existing = await db.execution_intents.find_one(
            {"dedupe_key": dedupe_key}, {"_id": 0})
        logger.warning("duplicate execution intent %s/%s key=%s → "
                       "returning original %s (%s)", source, kind,
                       dedupe_key, (existing or {}).get("intent_id"),
                       (existing or {}).get("status"))
        return {**(existing or {}), "duplicate": True}


async def transition(db, intent_id: str, to: str, detail: str = "",
                     result: dict | None = None) -> dict | None:
    """Move an intent along the legal lifecycle. Illegal transitions are
    REFUSED (returns None) — a terminal intent can never re-execute."""
    if to not in STATES:
        raise ValueError(f"unknown intent state: {to}")
    allowed_from = [s for s, t in _TRANSITIONS.items() if to in t]
    sets = {"status": to, "updated_at": _now()}
    if result is not None:
        sets["result"] = result
    upd = await db.execution_intents.find_one_and_update(
        {"intent_id": intent_id, "status": {"$in": allowed_from}},
        {"$set": sets,
         "$push": {"history": {"to": to, "at": _now(),
                               "detail": str(detail or "")[:200]}}},
        return_document=ReturnDocument.AFTER)
    if upd is None:
        logger.warning("refused illegal intent transition %s → %s",
                       intent_id, to)
        return None
    upd.pop("_id", None)
    return upd


async def run_once(db, *, source: str, kind: str, dedupe_key: str,
                   executor, payload: dict | None = None,
                   program_id: str | None = None,
                   account_id: str | None = None,
                   actor: str | None = None) -> dict:
    """At-most-once idempotency guard: mint the intent, atomically claim
    it, execute, record the result. Retries with the same dedupe_key get
    the stored outcome (or in_flight=True) — the executor never re-runs."""
    intent = await create_intent(
        db, source=source, kind=kind, dedupe_key=dedupe_key,
        payload=payload, program_id=program_id, account_id=account_id,
        actor=actor)
    iid = intent.get("intent_id")
    if intent.get("duplicate"):
        terminal = intent.get("status") in TERMINAL
        return {"intent_id": iid, "status": intent.get("status"),
                "duplicate": True, "in_flight": not terminal,
                "result": intent.get("result")}
    claimed = await db.execution_intents.find_one_and_update(
        {"intent_id": iid, "status": "created"},
        {"$set": {"status": "submitted", "updated_at": _now()},
         "$push": {"history": {"to": "submitted", "at": _now()}}})
    if claimed is None:  # lost the claim race — someone else is executing
        return {"intent_id": iid, "status": "submitted", "duplicate": True,
                "in_flight": True, "result": None}
    try:
        result = await executor(intent)
    except Exception as e:
        await transition(db, iid, "rejected", detail=str(e)[:200],
                         result={"error": str(e)[:300]})
        raise
    safe = result if isinstance(result, dict) else {"value": str(result)[:300]}
    safe.pop("_id", None)
    await transition(db, iid, "filled", result=safe)
    return {"intent_id": iid, "status": "filled", "duplicate": False,
            "in_flight": False, "result": safe}


async def expire_stale(db, older_than_sec: int = 900) -> int:
    """Sweep helper: non-terminal intents older than the window become
    EXPIRED. At-most-once holds — the dedupe key stays reserved."""
    cutoff = (datetime.now(timezone.utc)
              - timedelta(seconds=older_than_sec)).isoformat()
    r = await db.execution_intents.update_many(
        {"status": {"$in": ["created", "submitted", "acked"]},
         "created_at": {"$lt": cutoff}},
        {"$set": {"status": "expired", "updated_at": _now()},
         "$push": {"history": {"to": "expired", "at": _now(),
                               "detail": f"stale > {older_than_sec}s"}}})
    return r.modified_count
