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
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

logger = logging.getLogger("execution.intents")

STATES = ["created", "validated", "authorized", "submitted", "dispatched",
          "broker_pending", "acked", "acknowledged", "unknown", "filled",
          "reconciled", "rejected", "expired", "cancelled",
          "failed_confirmed"]
TERMINAL = {"filled", "reconciled", "rejected", "expired", "cancelled",
            "failed_confirmed"}
_TRANSITIONS = {
    "created": {"validated", "authorized", "submitted", "rejected",
                "expired", "cancelled"},
    "validated": {"authorized", "rejected", "expired", "cancelled"},
    "authorized": {"submitted", "dispatched", "rejected", "expired",
                   "cancelled"},
    "submitted": {"acked", "dispatched", "broker_pending", "filled",
                  "rejected", "expired", "cancelled", "unknown"},
    "dispatched": {"broker_pending", "acked", "acknowledged", "filled",
                   "rejected", "expired", "unknown"},
    "broker_pending": {"acked", "acknowledged", "filled", "rejected",
                       "expired", "unknown"},
    "acked": {"acknowledged", "filled", "rejected", "expired", "unknown"},
    "acknowledged": {"reconciled", "filled", "failed_confirmed"},
    # UNKNOWN never returns to a dispatchable state — broker truth decides
    "unknown": {"acknowledged", "reconciled", "failed_confirmed", "expired"},
}
# states that may still be expired safely (order NEVER left STOIC)
_PRE_DISPATCH = {"created", "validated", "authorized"}
# states where the order may have reached the broker — on staleness these
# become UNKNOWN and are resolved by broker-truth reconciliation only
_IN_FLIGHT = {"submitted", "dispatched", "broker_pending", "acked"}
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


async def record_late_fill(db, intent_id: str, *, ticket: int, via: str, prior: str = "") -> dict | None:
    """A6/H11 — broker truth: an order STOIC had already written off (expired /
    cancelled by PANIC or a lock) filled anyway. The intent becomes `filled` — the
    only exit from those terminal states, stamped `late_fill` so it is never mistaken
    for a normal lifecycle. In-flight intents go through the regular transition."""
    now = _now()
    # N12 — read the intent's REAL prior status (ReturnDocument.BEFORE); the caller's
    # `prior` hint is a trade-row field and may be stale or empty.
    before = await db.execution_intents.find_one_and_update(
        {"intent_id": intent_id, "status": {"$in": ["expired", "cancelled", "rejected", "unknown"]}},
        {"$set": {"status": "filled", "updated_at": now, "late_fill": True, "late_fill_via": via,
                  "result": {"ticket": int(ticket), "late_fill": True}}},
        return_document=ReturnDocument.BEFORE)
    if before is None:
        return await transition(db, intent_id, "filled", f"fill via {via} ticket={ticket}",
                                result={"ticket": int(ticket)})
    real_prior = str(before.get("status") or prior or "terminal")[:40]
    await db.execution_intents.update_one(
        {"intent_id": intent_id},
        {"$set": {"late_fill_prior_status": real_prior},
         "$push": {"history": {"to": "filled", "at": now,
                               "detail": f"late fill via {via} (was {real_prior}) ticket={ticket}"}}})
    if real_prior in ("rejected", "unknown"):
        # the broker said "rejected"/we never learned the outcome, yet a fill
        # arrived: broker truth wins, but this contradiction is surfaced, never silent.
        await db.execution_intents.update_one({"intent_id": intent_id}, {"$set": {"late_fill_anomaly": True}})
        try:
            from alerting import raise_alert
            await raise_alert(db, "late_fill_after_reject", "warning",
                              f"intent {intent_id} was {real_prior} but filled "
                              f"via {via} (ticket {ticket}) — verify the broker position and the EA journal",
                              dedup_key=f"late_fill_after_reject:{intent_id}",
                              meta={"intent_id": intent_id, "ticket": int(ticket), "via": via})
        except Exception as e:  # noqa: BLE001
            logger.warning("late-fill anomaly alert failed: %s", type(e).__name__)
    logger.warning("intent %s: late fill via %s (was %s) ticket=%s", intent_id, via, real_prior, ticket)
    upd = await db.execution_intents.find_one({"intent_id": intent_id}) or {**before, "status": "filled",
                                                                                 "late_fill_prior_status": real_prior}
    upd.pop("_id", None)
    return upd


class PreDispatchError(Exception):
    """Executors raise this when the action verifiably never left STOIC."""


def request_never_left(exc: BaseException) -> bool:
    """Classify an executor failure (v56 P0 correction).
    True  → safe to REJECT: the request never reached the wire, or the
            broker itself responded with an error (broker truth exists).
    False → POST-DISPATCH uncertainty: the request may have left STOIC —
            the intent must go UNKNOWN until broker truth decides.
    Unknown failure modes default to False: never infer safety."""
    if isinstance(exc, PreDispatchError):
        return True
    name = type(exc).__name__.lower()
    if "connecttimeout" in name or "connecterror" in name:
        return True  # TCP connect never established — nothing was sent
    if isinstance(exc, ConnectionRefusedError):
        return True
    if isinstance(exc, TimeoutError) or "timeout" in name \
            or "cancelled" in name:
        return False  # request may be in flight — POST_DISPATCH_TIMEOUT
    if isinstance(exc, (ValueError, PermissionError)):
        # adapters raise these from PARSED broker responses — the broker
        # answered, so the outcome is confirmed
        return True
    return False


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
        # merge error into any partial result the executor already stashed
        # (e.g. a trade_id written before the response was lost)
        cur = await db.execution_intents.find_one({"intent_id": iid},
                                                  {"result": 1})
        merged = dict((cur or {}).get("result") or {})
        merged["error"] = str(e)[:300]
        if request_never_left(e):
            # PRE_DISPATCH failure / broker-confirmed error → REJECTED
            await transition(db, iid, "rejected", detail=str(e)[:200],
                             result=merged)
        else:
            # POST_DISPATCH_TIMEOUT (v56 P0) — the broker may have
            # executed. Never claim rejection: UNKNOWN until broker truth.
            await transition(
                db, iid, "unknown",
                detail=f"post-dispatch failure "
                       f"({type(e).__name__}): {str(e)[:140]} — broker "
                       f"truth required, order will NOT be resent",
                result=merged)
            logger.warning("intent %s → UNKNOWN after post-dispatch "
                           "failure: %s", iid, e)
        raise
    safe = result if isinstance(result, dict) else {"value": str(result)[:300]}
    safe.pop("_id", None)
    await transition(db, iid, "filled", result=safe)
    return {"intent_id": iid, "status": "filled", "duplicate": False,
            "in_flight": False, "result": safe}


async def expire_stale(db, older_than_sec: int = 900) -> int:
    """Sweep helper: stale PRE-DISPATCH intents become EXPIRED (the order
    never left STOIC). In-flight intents are NEVER expired here — they go
    UNKNOWN via mark_unknown_stale and only broker truth resolves them.
    The dedupe key stays reserved either way (at-most-once holds)."""
    cutoff = (datetime.now(timezone.utc)
              - timedelta(seconds=older_than_sec)).isoformat()
    r = await db.execution_intents.update_many(
        {"status": {"$in": sorted(_PRE_DISPATCH)},
         "created_at": {"$lt": cutoff}},
        {"$set": {"status": "expired", "updated_at": _now()},
         "$push": {"history": {"to": "expired", "at": _now(),
                               "detail": f"stale > {older_than_sec}s"}}})
    return r.modified_count


async def mark_unknown_stale(db, older_than_sec: int = 300) -> int:
    """In-flight intents with no broker outcome inside the window enter
    UNKNOWN: STOIC does not know whether the broker executed. The order
    is NEVER resent — reconcile_unknown_intents queries stored broker
    truth and only then decides."""
    cutoff = (datetime.now(timezone.utc)
              - timedelta(seconds=older_than_sec)).isoformat()
    r = await db.execution_intents.update_many(
        {"status": {"$in": sorted(_IN_FLIGHT)},
         "created_at": {"$lt": cutoff}},
        {"$set": {"status": "unknown", "updated_at": _now()},
         "$push": {"history": {"to": "unknown", "at": _now(),
                               "detail": f"no broker outcome in "
                                         f"{older_than_sec}s"}}})
    if r.modified_count:
        logger.warning("%d execution intent(s) entered UNKNOWN — broker "
                       "reconciliation required, no resend",
                       r.modified_count)
    return r.modified_count


async def reconcile_unknown_intents(db, alert_after_sec: int = 900) -> dict:
    """UNKNOWN → query broker truth (the trade document carries the
    broker-reported outcome) → match identity → reconcile → decide.
    Unresolvable intents stay UNKNOWN and page the operator — STOIC never
    blindly resends."""
    from bson import ObjectId
    out = {"reconciled": 0, "failed_confirmed": 0, "still_unknown": 0}
    alert_cutoff = (datetime.now(timezone.utc)
                    - timedelta(seconds=alert_after_sec)).isoformat()
    async for it in db.execution_intents.find({"status": "unknown"}):
        trade_id = (it.get("result") or {}).get("trade_id")
        trade = None
        if trade_id:
            try:
                trade = await db.trades.find_one({"_id": ObjectId(trade_id)})
            except Exception:
                trade = None
        status = (trade or {}).get("status")
        if status in ("open", "closed"):  # broker DID execute
            await transition(db, it["intent_id"], "reconciled",
                             detail=f"broker truth: trade {status}, "
                                    f"ticket {(trade or {}).get('mt5_ticket')}")
            out["reconciled"] += 1
        elif status in ("failed", "cancelled", "rejected"):
            await transition(db, it["intent_id"], "failed_confirmed",
                             detail=f"broker truth: trade {status}")
            out["failed_confirmed"] += 1
        else:
            out["still_unknown"] += 1
            if (it.get("created_at", "") < alert_cutoff
                    and not it.get("unknown_alerted")):
                await db.execution_intents.update_one(
                    {"intent_id": it["intent_id"]},
                    {"$set": {"unknown_alerted": True}})
                await db.pamm_notifications.insert_one(
                    {"type": "ExecutionUnknown", "severity": "critical",
                     "at": _now(), "seen": False,
                     "intent_id": it["intent_id"],
                     "summary": f"Execution intent {it['intent_id']} is "
                                f"UNKNOWN for >{alert_after_sec}s — broker "
                                f"outcome unconfirmed, manual "
                                f"reconciliation required (order will NOT "
                                f"be resent)"})
    return out


@dataclass(frozen=True)
class CanonicalIntent:
    """v56 §2 — the one canonical object every trading source produces."""
    execution_intent_id: str
    account_id: str
    broker_account_number: str
    broker_server: str
    strategy_id: str
    strategy_version: str
    symbol: str
    side: str
    requested_volume: float
    stop_loss: float | None
    take_profit: float | None
    risk_snapshot_id: str
    signal_id: str
    created_at: str
    expires_at: str
    fencing_epoch: int
    nonce: str
    # v56 hardening — immutable decision-context references: five years
    # later STOIC can answer "why exactly was this trade permitted?"
    market_snapshot_id: str = ""
    authority_snapshot_id: str = ""
    broker_capability_version: str = ""
    model_version: str = ""
    execution_policy_version: str = ""
    # v62.3 — PAMM lineage: PAMM → assignment → strategy/version →
    # decision → intent → broker order → position → outcome
    pamm_program_id: str = ""
    assignment_id: str = ""
    strategy_hash: str = ""
    risk_profile_id: str = ""
    certification_id: str = ""


def canonical_payload(**kw) -> dict:
    kw.setdefault("execution_intent_id", "")
    kw.setdefault("created_at", _now())
    kw.setdefault("expires_at",
                  (datetime.now(timezone.utc)
                   + timedelta(seconds=120)).isoformat())
    return asdict(CanonicalIntent(**kw))
