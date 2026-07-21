"""Scalp subsystem · fast-path orchestrator.

Pipeline: regime permission → microstructure setup → calibrated probability
→ expected move → execution cost → conservative net expectancy → risk
approval → final fresh-quote check → order → reconciliation → attribution.

Hot-path guarantees (review items 5/6/7):
- NO awaited Mongo work between signal detection and order decision:
  decision IDs are local uuid4, persistence is fire-and-forget, the account
  document is preloaded on ingest.
- Signal freshness is measured from the INITIATING TICK's receive time,
  not a timestamp minted right before the gate.
- Immediately before submission the newest in-memory quote is rechecked for
  spread and adverse price drift; the EA's slippage veto (entry_price +
  max deviation) is the broker-side final gate.

Cost accounting (review item 1) — ONE consistent convention:
  entry exec  = ask + entry_slip (long)  /  bid − entry_slip (short)
  exit  exec  = executable side; exit slippage applied on STOP/TIMEOUT
                (market-out) but not on TARGET (limit fill)
  net_pips    = signed(exit_exec − entry_exec) − exit_slip_used − commission
  gross_move  = mid-to-mid reference move
Metrics consume outcome.net_pips DIRECTLY — expected costs are never
subtracted a second time.

Label hygiene (review items 10/11): every decision doc carries `dataset`
("candidate" for research labels, live fills are reconciled separately) and
overlapping same-direction candidates are suppressed while a sim is active.
"""
import asyncio
import logging
import os
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scalp import edge, gate, kill, permissions, setup
from scalp import adaptive_exits
from scalp import risk_reservations
from scalp import model as scalp_model
from eod_flatten import eod_flatten_block as _eod_flatten_block
from scalp.costs import dynamic_spread_limit
from scalp.features import snapshot
from scalp.feature_schema import FEATURE_SCHEMA_VERSION
from scalp.forecast import make as make_forecast
from scalp.instruments import approved
from scalp.risk import (ACCOUNT_LIMITS, DEFAULT_LIMITS, RiskState,
                        check as risk_check, check_account)
from scalp.state import ScalpState, TickEvent, now_ms

logger = logging.getLogger("scalp.engine")

EVAL_THROTTLE_MS = 1000
TICK_FLUSH_N = 200
TICK_FLUSH_MS = 10_000
RETRAIN_EVERY_RESOLUTIONS = 200
MAX_DRIFT_BEFORE_SUBMIT_FRAC = 0.5     # of stop distance, ABSOLUTE drift
MAX_BATCH_TRANSPORT_AGE_MS = 3000      # sent_at → arrival; older batches can't trade
AUDIT_BACKLOG_HALT = 500               # pending persist tasks that halt NEW entries
FAILED_ATTEMPT_COST_PIPS = 0.1         # opportunity/ops cost of a rejected order
MIN_FILL_ATTEMPTS_FOR_GATE = 20        # below this the fill-prob prior dominates

# Runner ownership is PROCESS-LOCAL (round 5 item 10): enforcement is a
# distributed account lease (round 7 item 1) — a worker must own
# scalp_owners:{account_id} before processing ticks or applying financials.
_owner_pid = os.getpid()
_worker_id = f"{os.uname().nodename}:{_owner_pid}"
LEASE_TTL_SEC = 30
RECONCILE_CLAIM_TTL_SEC = 60           # recovery-sweep per-deal claim TTL
# Round 14 item 10 — order-capacity protection: bound simultaneous broker
# submissions and runner construction per worker; excess fails closed.
MAX_CONCURRENT_SUBMISSIONS = int(os.environ.get(
    "SCALP_MAX_CONCURRENT_SUBMISSIONS", "8"))
MAX_RUNNERS_PER_WORKER = int(os.environ.get(
    "SCALP_MAX_RUNNERS_PER_WORKER", "400"))
_active_submissions = 0
# round 16 item 8 — final-commitment edge buffer for decision→submit latency
LATENCY_EDGE_BUFFER_PIPS = float(os.environ.get(
    "SCALP_LATENCY_EDGE_BUFFER_PIPS", "0.05"))
# round 17 item 1 — pre-submit quote validity: the last streamed tick must
# be recent, sequenced and two-sided or the commitment is refused outright.
MAX_SUBMIT_QUOTE_AGE_MS = int(os.environ.get(
    "SCALP_MAX_SUBMIT_QUOTE_AGE_MS", "3000"))
# round 17 item 2 — material-change thresholds beyond which the lightweight
# edge adjustment is NOT enough: regenerate the full forecast from a fresh
# feature snapshot (fresh probability, move, costs, geometry).
REQUOTE_DRIFT_FRAC = float(os.environ.get(
    "SCALP_REQUOTE_DRIFT_FRAC", "0.25"))          # of stop distance
REQUOTE_SPREAD_DELTA_PIPS = float(os.environ.get(
    "SCALP_REQUOTE_SPREAD_DELTA_PIPS", "0.3"))
# round 16 item 3 — MT5 OrderSend enforces margin broker-side and its
# rejects persist as attempt_failed; set false to hard-block unknown margin.
BROKER_MARGIN_PREFLIGHT = os.environ.get(
    "SCALP_BROKER_MARGIN_PREFLIGHT", "true").lower() == "true"
_lease_cache: dict = {}                # account_id -> (expires_epoch, owned)
_lease_epoch: dict = {}                # account_id -> fencing token we hold
# Round 13 item 9 — REASON-LEVEL account blocks: account_id -> set of reason
# codes. Every subsystem owns specific reason(s) and may only add/clear its
# own; one subsystem's clean pass can never clear another subsystem's block.
BLOCK_MISSING_PROTECTION = "missing_protection"
BLOCK_RISK_UNKNOWN = "risk_unknown"
BLOCK_INVARIANT = "invariant_violation"
BLOCK_DURABLE_INVARIANT = "durable_invariant"
BLOCK_LEDGER = "ledger_integrity"
BLOCK_PENDING_LEDGER = "pending_ledger_overdue"
_account_blocks: dict = {}
_runners: dict = {}
_account_risk: dict = {}               # account_id -> account-wide RiskState
_account_restored: set = set()         # account-level state loaded once
_audit_pending = 0
_audit_failures = 0


def account_risk_state(account_id: str) -> RiskState:
    rs = _account_risk.get(account_id)
    if rs is None:
        rs = RiskState(DEFAULT_LIMITS)
        _account_risk[account_id] = rs
    return rs


def add_account_block(account_id: str, reason: str) -> None:
    _account_blocks.setdefault(account_id, set()).add(reason)


def clear_account_block(account_id: str, reason: str) -> None:
    s = _account_blocks.get(account_id)
    if s is not None:
        s.discard(reason)
        if not s:
            _account_blocks.pop(account_id, None)


def account_block_reasons(account_id: str) -> set:
    return set(_account_blocks.get(account_id) or ())


def margin_audit(account: dict | None, symbol: str, lot: float,
                 price: float) -> dict:
    """Round 15 item 1 — explicit pre-submission margin audit. When broker
    leverage is unknown the check is recorded as unavailable (None), never
    silently assumed; the EA/broker remain the hard enforcement."""
    fm = (account or {}).get("free_margin")
    out = {"fresh_free_margin": float(fm) if fm is not None else None,
           "estimated_required_margin": None,
           "margin_utilization_after_order": None,
           "margin_check_passed": None}
    try:
        lev = float((account or {}).get("leverage") or 0)
    except (TypeError, ValueError):
        lev = 0.0
    # round 17 item 5 — the standard-lot FX formula is ONLY valid for the
    # approved scalp FX universe: any other instrument (metals, indices,
    # crypto, exotics) needs broker-native margin fields, so the backend
    # estimate is explicitly recorded as unavailable, never guessed.
    if approved(symbol) is None:
        out["reason"] = (f"backend margin model not approved for {symbol} — "
                         f"broker-native enforcement only")
        return out
    if lev > 0 and lot > 0 and price and fm is not None:
        contract = 100_000.0             # FX standard lot (scalp universe)
        req = lot * contract * float(price) / lev
        fm_f = float(fm)
        out["estimated_required_margin"] = round(req, 2)
        out["margin_check_passed"] = fm_f >= req * 1.2   # +20% buffer
        if fm_f > 0:
            out["margin_utilization_after_order"] = round(req / fm_f, 4)
        if not out["margin_check_passed"]:
            out["reason"] = (f"insufficient free margin: need ~${req:.2f} "
                             f"(+20% buffer), have ${fm_f:.2f}")
    return out


# Round 16 main — TRUE LEASED SEMAPHORE for distributed broker capacity:
# one slot document per unit of capacity. A crashed holder's lease simply
# EXPIRES (no counter reset can wipe capacity that is still legitimately in
# use), and only the holder's unique token can renew/release its own slot.
MAX_BROKER_CONCURRENT_SUBMISSIONS = int(os.environ.get(
    "SCALP_MAX_BROKER_CONCURRENT_SUBMISSIONS", "6"))
SUBMISSION_LEASE_SEC = int(os.environ.get(
    "SCALP_SUBMISSION_LEASE_SEC", "90"))
# round 16 item 11 — fairness: one account (or one symbol) can never
# monopolize the broker pool.
MAX_ACCOUNT_ACTIVE_SUBMISSIONS = int(os.environ.get(
    "SCALP_MAX_ACCOUNT_ACTIVE_SUBMISSIONS", "2"))
MAX_SYMBOL_ACTIVE_SUBMISSIONS = int(os.environ.get(
    "SCALP_MAX_SYMBOL_ACTIVE_SUBMISSIONS", "3"))
# round 17 item 9 — SEPARATE bounded pool for emergency risk-reducing ops:
# they never wait behind entries, but a widespread incident must not flood
# the broker either. Short leases double as queue-rate throttling.
EMERGENCY_MAX_CONCURRENT = int(os.environ.get(
    "SCALP_EMERGENCY_MAX_CONCURRENT", "12"))
EMERGENCY_LEASE_SEC = int(os.environ.get(
    "SCALP_EMERGENCY_LEASE_SEC", "20"))
# round 17 item 11 — slot↔trade invariant state (per broker_key): violations
# fail NEW entries closed until a clean sweep passes.
_capacity_violations: dict = {}
_slot_metrics = {"renewals": 0, "renewal_failures": 0, "orphan_releases": 0,
                 "terminal_releases": 0, "violations": 0,
                 "emergency_throttled": 0, "last_sweep_at": None}
_EPOCH_ISO = "1970-01-01T00:00:00+00:00"
_slots_ready: set = set()


def _broker_cap_key(broker: str, pool: str = "entry") -> str:
    base = f"broker:{(broker or 'unknown').strip().lower()}"
    return base if pool == "entry" else f"{pool}:{base}"


def _pool_size(pool: str) -> int:
    return (MAX_BROKER_CONCURRENT_SUBMISSIONS if pool == "entry"
            else EMERGENCY_MAX_CONCURRENT)


async def _ensure_submission_slots(db, broker_key: str,
                                   pool: str = "entry") -> None:
    n = _pool_size(pool)
    for i in range(n):
        await db.scalp_submission_slots.update_one(
            {"broker_key": broker_key, "slot_id": i},
            {"$setOnInsert": {"lease_until": _EPOCH_ISO,
                              "worker_id": None, "token": None}},
            upsert=True)
    # round 17 item 7 — a REDUCED pool must not leave stale higher-numbered
    # slots grantable; expired extras are deleted deliberately (live leases
    # are left to expire, then removed on the next ensure).
    await db.scalp_submission_slots.delete_many(
        {"broker_key": broker_key, "slot_id": {"$gte": n},
         "lease_until": {"$lt": datetime.now(timezone.utc).isoformat()}})


async def acquire_broker_submission_slot(db, broker: str,
                                         account_id: str = "",
                                         decision_id: str = "",
                                         symbol: str = "",
                                         pool: str = "entry") -> dict | None:
    """Returns a slot handle {broker_key, slot_id, token} or None when the
    pool (or a per-account / per-symbol fairness cap) is exhausted.
    Emergency closes / protection ops NEVER pass through the entry pool —
    they use their own bounded pool (pool='emergency', item 9)."""
    broker_key = _broker_cap_key(broker, pool)
    if broker_key not in _slots_ready:
        await _ensure_submission_slots(db, broker_key, pool)
        _slots_ready.add(broker_key)
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    async def _held(extra: dict) -> int:
        cnt = await db.scalp_submission_slots.count_documents(
            {"broker_key": broker_key, "lease_until": {"$gte": now_iso},
             **extra})
        return cnt if isinstance(cnt, int) else 0

    fair = pool == "entry"
    if fair and account_id and (await _held({"account_id": account_id})
                                >= MAX_ACCOUNT_ACTIVE_SUBMISSIONS):
        return None
    if fair and symbol and (await _held({"symbol": symbol})
                            >= MAX_SYMBOL_ACTIVE_SUBMISSIONS):
        return None
    token = uuid.uuid4().hex
    lease_sec = (SUBMISSION_LEASE_SEC if pool == "entry"
                 else EMERGENCY_LEASE_SEC)
    lease_until = (now + timedelta(seconds=lease_sec)).isoformat()
    for slot_id in range(_pool_size(pool)):
        res = await db.scalp_submission_slots.update_one(
            {"broker_key": broker_key, "slot_id": slot_id,
             "lease_until": {"$lt": now_iso}},
            {"$set": {"lease_until": lease_until, "token": token,
                      "worker_id": _worker_id, "account_id": account_id,
                      "symbol": symbol,
                      "decision_id": decision_id, "acquired_at": now_iso}})
        if bool(getattr(res, "modified_count", 0)):
            slot = {"broker_key": broker_key, "slot_id": slot_id,
                    "token": token}
            # round 17 item 8 — fairness check + claim are not atomic: two
            # workers can pass the pre-count together. COMPENSATE after the
            # claim: recount including our own slot; on overshoot, release
            # our own slot and deny. Brief overshoot self-corrects; the
            # global pool bound is never exceeded either way.
            if fair and account_id and (await _held(
                    {"account_id": account_id})
                    > MAX_ACCOUNT_ACTIVE_SUBMISSIONS):
                await release_broker_submission_slot(db, slot)
                return None
            if fair and symbol and (await _held({"symbol": symbol})
                                    > MAX_SYMBOL_ACTIVE_SUBMISSIONS):
                await release_broker_submission_slot(db, slot)
                return None
            return slot
    return None


async def renew_submission_slot(db, slot: dict) -> bool:
    """Long-running submissions renew while pending (token-fenced)."""
    if not slot:
        return False
    lease_until = (datetime.now(timezone.utc)
                   + timedelta(seconds=SUBMISSION_LEASE_SEC)).isoformat()
    res = await db.scalp_submission_slots.update_one(
        {"broker_key": slot["broker_key"], "slot_id": slot["slot_id"],
         "token": slot["token"]},
        {"$set": {"lease_until": lease_until}})
    return bool(getattr(res, "modified_count", 0))


async def release_broker_submission_slot(db, slot: dict | None) -> None:
    """Only the holder's token can release its own slot; a foreign release
    is a no-op. Crash without release → the lease expires harmlessly."""
    if not slot:
        return
    await db.scalp_submission_slots.update_one(
        {"broker_key": slot["broker_key"], "slot_id": slot["slot_id"],
         "token": slot["token"]},
        {"$set": {"lease_until": _EPOCH_ISO, "token": None}})


def round_to_tick(price: float, tick: float) -> float:
    """Round 17 item 6 — snap prices to the instrument tick grid instead of
    generic 5-decimal rounding (wrong for non-5-digit instruments)."""
    from decimal import ROUND_HALF_UP, Decimal
    if not tick or tick <= 0:
        return price
    q = Decimal(str(tick))
    return float((Decimal(str(price)) / q)
                 .to_integral_value(rounding=ROUND_HALF_UP) * q)


def _submission_terminal(trade: dict) -> bool:
    """A submission is terminal once the broker acknowledged it (ticket) or
    the order reached a final state — only then may its slot be freed."""
    if trade.get("mt5_ticket"):
        return True
    return str(trade.get("status") or "") in (
        "open", "closed", "rejected", "failed", "cancelled", "expired")


def capacity_integrity_reason(broker_key: str | None = None) -> str | None:
    if broker_key is not None:
        v = _capacity_violations.get(broker_key)
        return v and v.get("reason")
    for v in _capacity_violations.values():
        if v:
            return v.get("reason")
    return None


async def sweep_submission_slots(db) -> dict:
    """Round 17 main — slot↔trade lifecycle sweep (runs every ~lease/3):

    1. RENEW leases of slots whose linked trade is still awaiting broker
       acknowledgement (token-fenced; works cross-worker because the token
       is persisted on the trade doc).
    2. RELEASE slots whose linked trade reached a terminal submission state
       and slots that no trade references (orphans past a grace period).
    3. INVARIANTS: a nonterminal pending order whose recorded token no
       longer owns its slot, or a token owning >1 slot, is a capacity
       integrity violation → NEW entries for that broker fail closed until
       a clean sweep passes."""
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    grace_iso = (now - timedelta(seconds=30)).isoformat()
    report = {"renewed": 0, "released_terminal": 0, "released_orphan": 0,
              "violations": []}
    seen_tokens: dict = {}
    live_by_token: dict = {}
    async for slot in db.scalp_submission_slots.find(
            {"lease_until": {"$gte": now_iso}, "token": {"$ne": None}}):
        handle = {"broker_key": slot["broker_key"],
                  "slot_id": slot["slot_id"], "token": slot["token"]}
        if slot["token"] in seen_tokens:
            report["violations"].append(
                {"type": "token_owns_multiple_slots",
                 "broker_key": slot["broker_key"], "token": slot["token"]})
            continue
        seen_tokens[slot["token"]] = handle
        live_by_token[slot["token"]] = slot
        trade = await db.trades.find_one(
            {"submission_slot.token": slot["token"]},
            {"status": 1, "mt5_ticket": 1})
        if trade is None:
            if str(slot.get("acquired_at") or "") < grace_iso:
                await release_broker_submission_slot(db, handle)
                _slot_metrics["orphan_releases"] += 1
                report["released_orphan"] += 1
            continue
        if _submission_terminal(trade):
            await release_broker_submission_slot(db, handle)
            _bg(lambda tid=trade["_id"]: db.trades.update_one(
                {"_id": tid}, {"$unset": {"submission_slot": ""}}),
                "slot_unlink")
            _slot_metrics["terminal_releases"] += 1
            report["released_terminal"] += 1
        else:
            if await renew_submission_slot(db, handle):
                _slot_metrics["renewals"] += 1
                report["renewed"] += 1
            else:
                _slot_metrics["renewal_failures"] += 1
    # nonterminal pending orders must still own their recorded slot; an
    # EXPIRED lease is recoverable (re-renew via the persisted token), a
    # token overtaken by a different holder is a hard violation.
    async for tr in db.trades.find(
            {"scope": "scalp_fast", "status": "pending",
             "submission_slot.token": {"$exists": True}},
            {"submission_slot": 1, "status": 1, "mt5_ticket": 1}):
        ref = tr["submission_slot"]
        cur = await db.scalp_submission_slots.find_one(
            {"broker_key": ref["broker_key"], "slot_id": ref["slot_id"]})
        if cur is None:
            continue
        if cur.get("token") == ref.get("token"):
            if str(cur.get("lease_until") or "") < now_iso:
                if await renew_submission_slot(db, ref):     # crashed worker
                    _slot_metrics["renewals"] += 1
                    report["renewed"] += 1
        elif cur.get("token") is not None:
            report["violations"].append(
                {"type": "pending_order_lost_slot",
                 "broker_key": ref["broker_key"], "trade_id": str(tr["_id"])})
    by_broker: dict = {}
    for v in report["violations"]:
        by_broker.setdefault(v["broker_key"], []).append(v)
    for bk, vs in by_broker.items():
        _capacity_violations[bk] = {
            "reason": f"capacity integrity violated: {vs[0]['type']}",
            "violations": vs, "at": now_iso}
        _slot_metrics["violations"] += len(vs)
        logger.critical("submission-slot invariant violations %s: %s", bk, vs)
    for bk in [k for k in list(_capacity_violations) if k not in by_broker]:
        _capacity_violations.pop(bk, None)
    _slot_metrics["last_sweep_at"] = now_iso
    return report


def update_account_snapshot(account_id: str, snapshot: dict) -> None:
    """Round 14 P0 — every heartbeat propagates the FRESH account snapshot
    into all in-memory runners for the account. Without this, staleness
    checks and sizing use whatever account doc the last tick batch preloaded:
    a runner could false-block on an old last_heartbeat, or size on stale
    equity, even while heartbeats keep landing in MongoDB."""
    for r in runners_for_account(account_id):
        r.account = {**(r.account or {}), **snapshot}
        eq = snapshot.get("equity")
        if eq is not None:
            r.equity = float(eq)


def set_protection_block(account_id: str, blocked: bool) -> None:
    """Round 8 — while an account has ANY unprotected open position, new
    scalp entries are refused (flag maintained by protection_guard sweep)."""
    if blocked:
        add_account_block(account_id, BLOCK_MISSING_PROTECTION)
    else:
        clear_account_block(account_id, BLOCK_MISSING_PROTECTION)


def verify_account_invariants(account_id: str) -> list:
    """Round 8 item 9 — in-memory reconciliation invariants. Violations
    block new entries for the account until a clean pass."""
    violations = []
    runners = runners_for_account(account_id)
    if not runners:
        clear_account_block(account_id, BLOCK_INVARIANT)
        return violations
    ars = account_risk_state(account_id)
    open_total = sum(len(r.live_trades) for r in runners)
    if ars.open_scalps != open_total:
        violations.append(
            f"account open count {ars.open_scalps} != live trades {open_total}")
    trade_risk = 0.0
    for r in runners:
        for tid, info in r.live_trades.items():
            trade_risk += float(info.get("stop_risk_usd") or
                                ars.stop_risk_by_trade.get(tid, 0.0))
            if not info.get("stop_px"):
                violations.append(f"open scalp {tid} has no protective stop")
    acct_risk = sum(v for k, v in ars.stop_risk_by_trade.items()
                    if k != "unprotected_positions")
    # round 9 item 9 — tolerance scaled to rounding precision, not 5%
    tol = max(0.01, 0.0001 * max(acct_risk, trade_risk, 1.0))
    if abs(acct_risk - trade_risk) > tol:
        violations.append(
            f"account stop risk ${acct_risk:.2f} != sum of open trade "
            f"stop risks ${trade_risk:.2f} (tol ${tol:.2f})")
    if violations:
        add_account_block(account_id, BLOCK_INVARIANT)
        logger.error("scalp invariant violations on %s: %s",
                     account_id, violations)
    else:
        clear_account_block(account_id, BLOCK_INVARIANT)
    return violations


async def acquire_account_lease(db, account_id: str,
                                worker_id: str | None = None,
                                ttl_sec: int = LEASE_TTL_SEC) -> bool:
    """Round 7 item 1 — distributed account ownership. Atomically claims
    scalp_owners:{account_id} when this worker already owns it or the lease
    expired. A worker that does NOT own the account must not process ticks,
    create orders or apply financials for it."""
    wid = worker_id or _worker_id
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    lease = {"worker_id": wid,
             "lease_until": (now + timedelta(seconds=ttl_sec)).isoformat(),
             "heartbeat": now_iso}
    # round 8 item 2 — monotonic fencing token: every ownership CHANGE bumps
    # lease_epoch; stale workers carry an older epoch and their persisted
    # writes are rejected (see persist_risk_now).
    res = await db.scalp_owners.update_one(
        {"account_id": account_id, "worker_id": wid},
        {"$set": lease})
    if res.matched_count:                      # renewal — epoch unchanged
        return True
    res = await db.scalp_owners.update_one(
        {"account_id": account_id, "lease_until": {"$lt": now_iso}},
        {"$set": lease, "$inc": {"lease_epoch": 1}})
    if res.matched_count:
        doc = await db.scalp_owners.find_one({"account_id": account_id})
        if doc:
            _lease_epoch[account_id] = int(doc.get("lease_epoch") or 0)
        return True
    doc = await db.scalp_owners.find_one({"account_id": account_id})
    if doc is None:
        await db.scalp_owners.update_one(
            {"account_id": account_id},
            {"$setOnInsert": {**lease, "lease_epoch": 1}}, upsert=True)
        doc = await db.scalp_owners.find_one({"account_id": account_id})
    owned = bool(doc and doc.get("worker_id") == wid)
    if owned and doc:
        _lease_epoch[account_id] = int(doc.get("lease_epoch") or 0)
    return owned


async def ensure_account_lease(db, account_id: str) -> bool:
    """Locally-cached lease check: one DB round-trip per ~half TTL keeps the
    tick hot path free of per-batch database reads."""
    import time as _t
    cached = _lease_cache.get(account_id)
    if cached and _t.time() < cached[0]:
        return cached[1]
    owned = await acquire_account_lease(db, account_id)
    _lease_cache[account_id] = (_t.time() + LEASE_TTL_SEC / 2, owned)
    return owned


async def confirm_account_lease_now(db, account_id: str) -> tuple:
    """Round 10 item 1 — NON-CACHED ownership confirmation for order
    execution. The cached helper can return a stale owned=True for up to
    half the lease TTL; a worker that paused past its lease while another
    worker acquired a newer fencing epoch could still submit a broker order
    (fencing protects later DB writes but cannot undo a submitted order).
    This check always reads the ownership record. Returns (owned, epoch)."""
    now = datetime.now(timezone.utc).isoformat()
    doc = await db.scalp_owners.find_one(
        {"account_id": account_id, "worker_id": _worker_id,
         "lease_until": {"$gt": now}},
        {"lease_epoch": 1})
    if not doc:
        return False, 0
    epoch = int(doc.get("lease_epoch") or 0)
    _lease_epoch[account_id] = epoch
    return True, epoch


_service_block_reason: str | None = None


def set_service_block(reason: str | None) -> None:
    """Round 10 item 5 — scalp-critical startup failures (e.g. unique-index
    creation) fail the WHOLE scalp service closed: no new entries anywhere
    until the block is cleared."""
    global _service_block_reason
    _service_block_reason = reason


def service_block_reason() -> str | None:
    return _service_block_reason


# Round 12 item 9 — invariant-scan health telemetry. A stale or failing
# integrity scan is itself a risk condition: entries are vetoed when the
# last successful sweep is older than the grace window.
_invariant_scan: dict = {"last_attempt_at": None, "last_success_at": None,
                         "last_error": None, "last_duration_ms": None,
                         "docs_examined": 0, "blocked_accounts": 0,
                         "ledger_mismatches": 0,
                         "oldest_pending_event_sec": None}
INVARIANT_SCAN_STALE_SEC = int(os.environ.get(
    "SCALP_INVARIANT_SCAN_STALE_SEC", "900"))
PENDING_EVENT_MAX_AGE_SEC = int(os.environ.get(
    "SCALP_PENDING_EVENT_MAX_AGE_SEC", "600"))


def invariant_scan_stale_reason() -> str | None:
    """Veto reason when integrity scanning has run before but has not
    SUCCEEDED within the grace window (never blocks environments where the
    scheduler hasn't started scanning at all)."""
    attempted = _invariant_scan.get("last_attempt_at")
    success = _invariant_scan.get("last_success_at")
    if not attempted:
        return None
    ref = success or attempted
    try:
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(ref)).total_seconds()
    except ValueError:
        return None
    if success is None or age > INVARIANT_SCAN_STALE_SEC:
        return (f"invariant scan stale ({int(age)}s since last success; "
                f"last_error={_invariant_scan.get('last_error')})")
    return None


# Round 13 item 8 — readiness requires a FRESH broker account snapshot: the
# heartbeat feeds equity/balance/positions, and sizing on a stale snapshot
# is trading blind. Entries are vetoed while broker state is stale.
BROKER_STATE_STALE_SEC = int(os.environ.get(
    "SCALP_BROKER_STATE_STALE_SEC", "90"))


def broker_state_stale_reason(account: dict | None) -> str | None:
    if not account:
        return "broker state unknown (no account snapshot)"
    hb = account.get("last_heartbeat")
    if not hb:
        return "broker state stale (no heartbeat recorded)"
    try:
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(str(hb))).total_seconds()
    except ValueError:
        return "broker state stale (unparseable heartbeat timestamp)"
    if age > BROKER_STATE_STALE_SEC:
        return f"broker state stale (last heartbeat {int(age)}s ago)"
    status = str(account.get("status") or "")
    if status and status != "connected":
        return f"broker state not connected (status={status})"
    return None


async def confirm_or_adopt_account_lease(db, account_id: str) -> bool:
    """Round 12 item 2 — LIVE-callback ownership check. Confirms current
    ownership; adopts the account ONLY when no ownership record exists at
    all (first touch). Never takes over another worker's expired lease —
    that is the recovery sweep's clearly-logged job."""
    owned, _ = await confirm_account_lease_now(db, account_id)
    if owned:
        return True
    doc = await db.scalp_owners.find_one(
        {"account_id": account_id}, {"worker_id": 1})
    if doc is None:
        return await acquire_account_lease(db, account_id)
    return False


async def acquire_expired_ownership_for_recovery(db, account_id: str) -> bool:
    """Round 12 item 2 — RECOVERY takeover of an expired lease, explicitly
    logged so ownership transitions are auditable."""
    prev = await db.scalp_owners.find_one(
        {"account_id": account_id}, {"worker_id": 1, "lease_epoch": 1})
    ok = await acquire_account_lease(db, account_id)
    if ok and prev is not None and prev.get("worker_id") != _worker_id:
        logger.warning(
            "RECOVERY TAKEOVER account=%s from worker=%s (epoch %s → %s) "
            "by %s", account_id, prev.get("worker_id"),
            prev.get("lease_epoch"), _lease_epoch.get(account_id),
            _worker_id)
    return ok


async def _restore_account_state(db, account_id: str, force: bool = False):
    """Round 7 item 5 — ONE account-level initialization: load account risk,
    scan ALL open scalp trades once, rebuild monetary stop risk and the open
    count. Symbol runners no longer independently reconstruct the account."""
    if account_id in _account_restored and not force:
        return
    _account_restored.add(account_id)
    ars = account_risk_state(account_id)
    doc = await db.scalp_risk_state.find_one(
        {"account_id": account_id, "symbol": "_ACCOUNT"})
    if doc:
        ars.load_doc(doc)
    ars.stop_risk_by_trade.clear()
    from pip_utils import pip_value_usd_per_lot_strict
    risk_unknown = False
    open_count = 0
    # round 13 item 1 — FULL cursor: a truncated .to_list(N) would silently
    # drop open positions past N from the restored risk picture
    async for tr in db.trades.find(
            {"account_id": account_id, "scope": "scalp_fast",
             "status": "open"}):
        open_count += 1
        lot = float(tr.get("lot_size") or 0)
        entry = tr.get("entry_price")
        stop = tr.get("stop_loss")
        cfg = approved(tr.get("symbol") or "")
        pip = cfg.pip_size if cfg else 0.0001
        if lot > 0 and entry and stop:
            # round 10 item 3 — never silently assume $10/pip: an
            # unpriceable symbol marks account risk UNKNOWN and blocks
            # new entries instead of restoring a fabricated number.
            pv = pip_value_usd_per_lot_strict(tr.get("symbol") or "EURUSD")
            if pv is None:
                risk_unknown = True
                logger.error(
                    "risk restore: no authoritative pip value for %s "
                    "(trade %s) — account %s risk marked UNKNOWN, "
                    "entries blocked", tr.get("symbol"), tr["_id"],
                    account_id)
                continue
            ars.add_stop_risk(str(tr["_id"]),
                              lot * abs(float(entry) - float(stop)) / pip * pv)
    if risk_unknown:
        add_account_block(account_id, BLOCK_RISK_UNKNOWN)
    else:
        clear_account_block(account_id, BLOCK_RISK_UNKNOWN)
    ars.open_scalps = open_count


class ShadowSim:
    """Barrier-label tracker with explicit gross/cost/net accounting."""

    def __init__(self, decision_id, direction, entry_mid, entry_exec,
                 target_pips, stop_pips, pip, opened_ms, max_holding_ms,
                 exit_slippage_pips=0.0, commission_pips=0.0):
        self.decision_id = decision_id
        self.direction = direction
        self.entry_mid = entry_mid
        self.entry_exec = entry_exec
        self.pip = pip
        self.opened_ms = opened_ms
        self.deadline_ms = opened_ms + max_holding_ms
        self.exit_slippage_pips = exit_slippage_pips
        self.commission_pips = commission_pips
        if direction == "BUY":
            self.target_px = entry_exec + target_pips * pip
            self.stop_px = entry_exec - stop_pips * pip
        else:
            self.target_px = entry_exec - target_pips * pip
            self.stop_px = entry_exec + stop_pips * pip

    def advance(self, t: TickEvent):
        """Long exits at BID, short exits at ASK. Returns outcome dict or None."""
        px = t.bid if self.direction == "BUY" else t.ask
        if self.direction == "BUY":
            if px <= self.stop_px:
                return self._done("stop_first", px, t)
            if px >= self.target_px:
                return self._done("target_first", px, t)
        else:
            if px >= self.stop_px:
                return self._done("stop_first", px, t)
            if px <= self.target_px:
                return self._done("target_first", px, t)
        if t.broker_time_ms >= self.deadline_ms:
            return self._done("timeout", px, t)
        return None

    def _done(self, result, px, t):
        # exit slippage: market-out exits (stop/timeout) slip; limit target doesn't
        exit_slip = self.exit_slippage_pips if result in ("stop_first", "timeout") else 0.0
        signed_exec = ((px - self.entry_exec) if self.direction == "BUY"
                       else (self.entry_exec - px)) / self.pip
        exit_mid = (t.bid + t.ask) / 2.0
        gross = ((exit_mid - self.entry_mid) if self.direction == "BUY"
                 else (self.entry_mid - exit_mid)) / self.pip
        net = signed_exec - exit_slip - self.commission_pips
        return {
            "result": result,
            "gross_move_pips": round(gross, 2),
            "spread_cost_pips": round(abs(self.entry_exec - self.entry_mid) / self.pip
                                      + abs(exit_mid - px) / self.pip, 2),
            "exit_slippage_pips": round(exit_slip, 2),
            "commission_pips": round(self.commission_pips, 2),
            "net_pips": round(net, 2),
            "time_to_exit_ms": int(t.broker_time_ms - self.opened_ms),
            "exit_px": px,
            "resolved_at": datetime.now(timezone.utc).isoformat(),
        }


class ScalpRunner:
    def __init__(self, account_id: str, user_id: str, symbol: str):
        self.account_id = account_id
        self.user_id = user_id
        self.symbol = symbol
        self.cfg = approved(symbol)
        self.state = ScalpState(self.cfg.pip_size)
        self.risk_state = RiskState(DEFAULT_LIMITS)
        self.account_risk = account_risk_state(account_id)
        self._applied_deal_ids: deque = deque(maxlen=500)
        self._financials_applied: deque = deque(maxlen=500)
        self._closed_awaiting_financials: dict = {}
        self.exec_attempts = 0
        self.exec_fills = 0
        self.last_order_ack_ms: int | None = None
        self.commission_check: dict | None = None
        self._last_financial_event: dict | None = None
        self.account: dict | None = None       # preloaded on ingest (item 5)
        self.equity = 0.0
        self.mode = "shadow"
        self.enabled = False
        self.commission_usd_per_lot_side = 0.0
        self.broker = ""
        self.account_type = ""
        self.health = {"status": "OK", "open_allowed": False, "reasons": []}
        self.open_sims: list[ShadowSim] = []
        self.live_trades: dict = {}            # trade_id -> {state: QUEUED|OPEN|CLOSE_REQUESTED,...}
        self._hydrated = False
        self._risk_restored = False            # NO entries until restored (item 9)
        self.ack_ms_recent: deque = deque(maxlen=20)   # broker fill-delay history
        self._last_eval_ms = 0
        self._tick_buffer: list = []
        self._last_flush_ms = now_ms()
        self._resolutions_since_retrain = 0
        self.counters = {"ticks": 0, "evals": 0, "candidates": 0,
                         "suppressed_overlap": 0,
                         "shadow_trades": 0, "live_trades": 0, "rejected": 0}
        self.last_decision: dict | None = None

    def model_key(self) -> str:
        return scalp_model.make_key(self.broker, self.account_type, self.symbol)

    # ---------------- ingestion ----------------

    async def ingest(self, db, account: dict, ticks: list[dict],
                     sent_at_ms: int | None) -> dict:
        self.account = account
        self.equity = float(account.get("equity") or 0)
        self.broker = str(account.get("broker") or "")
        self.account_type = str(account.get("account_type") or "")
        recv = now_ms()
        # round 4 items 3/5: transport age known BEFORE processing; missing
        # sender timestamp is UNKNOWN, not fresh — fail closed for entries.
        transport_age = (recv - int(sent_at_ms)) if sent_at_ms else None
        trusted = transport_age is not None and transport_age <= MAX_BATCH_TRANSPORT_AGE_MS
        # round 4 item 4: ordering watermark spans BATCHES, not just this one
        last_tm = (self.state.last_tick.broker_time_ms
                   if self.state.last_tick else None)
        for raw in ticks:
            try:
                tm = int(raw["tm"])
                if last_tm is not None and tm <= last_tm:
                    continue                    # duplicate / out-of-order / old batch
                last_tm = tm
                t = TickEvent(symbol=self.symbol, broker_time_ms=tm,
                              received_time_ms=recv,
                              bid=float(raw["b"]), ask=float(raw["a"]))
            except (KeyError, TypeError, ValueError):
                continue
            self.state.update(t, trusted=trusted)
            self.counters["ticks"] += 1
            self._advance_sims(db, t)
            self._monitor_live_exits(db, t)
            self._tick_buffer.append({"tm": t.broker_time_ms, "b": t.bid, "a": t.ask})
        self.health = kill.evaluate(self.state, self.cfg)
        permissions.maybe_refresh(db, self.user_id, self.symbol, self.cfg)
        self._maybe_flush_ticks(db)
        # Delayed-batch guard (round 3): old broker ticks arriving NOW must
        # not look fresh. New entries require BOTH recent transport AND a
        # recent broker-market timestamp (clock-offset corrected).
        broker_age = self.state.broker_adjusted_age_ms()
        batch_fresh = (trusted and broker_age <= self.cfg.max_quote_age_ms)
        if not batch_fresh:
            self.counters["stale_batches"] = self.counters.get("stale_batches", 0) + 1
        audit_ok = _audit_pending < AUDIT_BACKLOG_HALT
        if self.enabled and self._risk_restored and batch_fresh and audit_ok:
            await self._maybe_evaluate(db)
        return {"ok": True, "ticks": len(ticks),
                "health": self.health["status"], "enabled": self.enabled,
                "risk_restored": self._risk_restored,
                "batch_fresh": batch_fresh,
                "transport_age_ms": transport_age,
                "trusted": trusted,
                "broker_age_ms": broker_age}

    # ---------------- decision pipeline ----------------

    def _commission_pips(self, lot_neutral: bool = True) -> float:
        if self.commission_usd_per_lot_side <= 0:
            return 0.0
        from pip_utils import pip_value_usd_per_lot_strict
        pv = pip_value_usd_per_lot_strict(self.symbol)
        if pv is None:
            return 999.0        # unpriceable symbol → cost vetoes any entry
        return round(self.commission_usd_per_lot_side * 2.0 / pv, 3)

    async def _maybe_evaluate(self, db):
        nm = now_ms()
        if nm - self._last_eval_ms < EVAL_THROTTLE_MS:
            return
        self._last_eval_ms = nm
        self.counters["evals"] += 1

        feats = snapshot(self.state)
        if feats is None:
            return
        perms = permissions.get_cached(self.user_id, self.symbol)
        cand = setup.detect(feats, self.state)
        if cand is None:
            return
        direction = cand["direction"]
        # item 3 (round 3) — ONE active setup event per symbol: any live sim
        # (either direction) suppresses new labels against the same path
        if self.open_sims:
            self.counters["suppressed_overlap"] += 1
            return
        self.counters["candidates"] += 1
        perm_ok = (perms.get("long_enabled") if direction == "BUY"
                   else perms.get("short_enabled"))

        commission_pips = self._commission_pips()
        pred = scalp_model.predict(self.model_key(), feats)
        model_p = pred["p"]
        fc = make_forecast(feats, cand, self.state, self.cfg,
                           model_p=model_p, commission_pips=commission_pips)
        edge_res = edge.evaluate(fc)

        from pip_utils import pip_value_usd_per_lot_strict
        pip_val = pip_value_usd_per_lot_strict(self.symbol)
        # round 6 items 4/5 — EXPLICIT account-level policy: open-position
        # count AND monetary stop-risk budget across ALL runners.
        # round 8 — protection/invariant blocks veto entries outright.
        account_open = sum(len(r.live_trades) for r in runners_for_account(self.account_id))
        if _service_block_reason:
            risk_res = {"ok": False, "lot": 0.0,
                        "reason": f"scalp service blocked: {_service_block_reason}"}
        elif (scan_stale := invariant_scan_stale_reason()) is not None:
            # round 12 item 9 — a stale/failed integrity scan fails closed
            risk_res = {"ok": False, "lot": 0.0, "reason": scan_stale}
        elif pip_val is None:
            # round 11 item 6 — no authoritative pip value → risk UNKNOWN
            risk_res = {"ok": False, "lot": 0.0,
                        "reason": (f"no authoritative pip value for "
                                   f"{self.symbol} — risk unknown, entry "
                                   f"blocked")}
        elif (stale_reason := broker_state_stale_reason(self.account)) is not None:
            # round 13 item 8 — readiness requires FRESH broker state
            risk_res = {"ok": False, "lot": 0.0, "reason": stale_reason}
        elif (eod_reason := _eod_flatten_block(self.account)) is not None:
            # iter-53 EOD flatten — no new scalps while positions are being
            # closed ahead of the broker's daily rollover
            risk_res = {"ok": False, "lot": 0.0, "reason": eod_reason}
        elif (block_reasons := account_block_reasons(self.account_id)):
            # round 13 item 9 — reason-level blocks: every owning subsystem
            # must clear its own reason before entries resume
            risk_res = {"ok": False, "lot": 0.0,
                        "reason": ("account blocked: "
                                   + ", ".join(sorted(block_reasons)))}
        elif account_open >= ACCOUNT_LIMITS.max_total_open_positions:
            risk_res = {"ok": False, "lot": 0.0,
                        "reason": "account-level max open scalp positions"}
        else:
            risk_res = risk_check(self.risk_state, self.equity, fc.stop_pips,
                                  pip_val, self.cfg)
            if risk_res["ok"]:
                proposed_stop_risk = (risk_res["lot"] * fc.stop_pips
                                      * pip_val)
                acct_check = check_account(
                    self.account_risk, self.equity,
                    proposed_stop_risk_usd=proposed_stop_risk)
                if not acct_check["ok"]:
                    risk_res = {"ok": False, "lot": 0.0,
                                "reason": acct_check["reason"]}
        spread_limit = dynamic_spread_limit(self.state, self.cfg)
        # item 6 — freshness measured from the INITIATING tick
        signal_ts_ms = self.state.last_tick.received_time_ms
        gate_res = gate.final_execution_gate(
            self.state, self.cfg, edge_res["net_edge_pips"],
            signal_ts_ms=signal_ts_ms, spread_limit_pips=spread_limit,
            health_open_allowed=self.health["open_allowed"])

        all_ok = bool(perm_ok) and edge_res["ok"] and risk_res["ok"] and gate_res["ok"]
        if all_ok and self.mode == "demo_live":
            verdict = "live_traded"
        elif all_ok:
            verdict = "shadow_traded"
        else:
            verdict = "rejected"
            self.counters["rejected"] += 1

        t = self.state.last_tick
        entry_mid = (t.bid + t.ask) / 2.0
        entry_exec = ((t.ask + fc.expected_slippage_pips * self.cfg.pip_size)
                      if direction == "BUY"
                      else (t.bid - fc.expected_slippage_pips * self.cfg.pip_size))

        decision_id = uuid.uuid4().hex           # item 5 — local ID, no DB wait
        doc = {
            "decision_id": decision_id,
            "user_id": self.user_id, "account_id": self.account_id,
            "symbol": self.symbol,
            "broker": self.broker, "account_type": self.account_type,
            "model_key": self.model_key(),
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "model_version": scalp_model.version_of(self.model_key()),
            "dataset": "candidate",
            "ts_ms": nm, "signal_ts_ms": signal_ts_ms,
            "feature_snapshot_ts_ms": feats["_now_ms"],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "direction": direction, "mode": self.mode,
            "features": {k: v for k, v in feats.items() if not k.startswith("_")},
            "setup": cand, "forecast": fc.to_dict(),
            "model_source": pred["source"],
            "model_fallback_reason": pred["fallback_reason"],
            "net_edge_pips": edge_res["net_edge_pips"],
            "cost_pips": edge_res["cost_pips"],
            "cost_ratio": edge_res["cost_ratio"],
            "gates": {
                "permission": {"ok": bool(perm_ok), "regime": perms.get("regime"),
                               "reasons": perms.get("reasons", [])},
                "edge": edge_res, "risk": risk_res, "final": gate_res,
            },
            "verdict": verdict,
            "lot": risk_res.get("lot", 0.0),
            # round 16 item 9 — explicit mode fields (never inferred from
            # dataset names): shadow sims vs demo vs true-live fills.
            "execution_mode": ("shadow" if verdict != "live_traded"
                               else ("live" if self.account_type == "live"
                                     else "demo")),
            "outcome_source": ("simulated" if verdict != "live_traded"
                               else "broker"),
            "submission_status": "not_submitted",
            # round 14 item 2 — the exact account snapshot behind this
            # decision, so any approved lot size is reproducible later.
            "account_snapshot": {
                "equity": self.equity,
                "free_margin": (self.account or {}).get("free_margin"),
                "heartbeat_at": (self.account or {}).get("last_heartbeat"),
                "status": (self.account or {}).get("status"),
                "symbol_specs_updated_at":
                    (self.account or {}).get("symbol_specs_updated_at"),
            },
            "sim": {"entry_mid": entry_mid, "entry_exec": entry_exec,
                    "target_pips": fc.target_pips, "stop_pips": fc.stop_pips},
            "outcome": None,
        }
        # Phase-1 · $ EV + Trade Quality Score (observe-only) + quality-scaled
        # sizing (downscale-only: the scalp risk budget stays the ceiling).
        try:
            from trade_quality import (compute_ev, quality_score,
                                       scalp_size_multiplier)
            _ev = compute_ev(fc.p_target_before_stop, fc.target_pips,
                             fc.stop_pips, float(edge_res["cost_pips"] or 0),
                             pip_val, risk_res.get("lot") or 0.0)
            _q = quality_score(
                ev_pips=_ev["ev_pips"], cost_pips=_ev["cost_pips"],
                p_win=_ev["p_win"],
                trend_alignment=(min(1.0, abs(float(
                    perms.get("ema_slope_pips") or 0)))
                    if perm_ok else 0.0),
                liquidity=min(1.0, float(feats.get("tick_rate") or 0) / 1.5),
                spread_ratio=(float(feats["spread_pips"]) / spread_limit
                              if spread_limit else None),
                volatility_ratio=(float(feats.get("vol_short") or 0)
                                  / float(feats["vol_long"])
                                  if feats.get("vol_long") else None),
                hour_utc=datetime.now(timezone.utc).hour)
            doc["ev"] = _ev
            doc["quality"] = _q
            # refinement 6 — consolidated decision metadata for optimisation
            self.state.vols.append(float(feats.get("vol_short") or 0))
            _vs = sorted(self.state.vols)
            _v_now = float(feats.get("vol_short") or 0)
            doc["decision_meta"] = {
                "ev_usd": _ev.get("ev_usd"), "ev_pips": _ev.get("ev_pips"),
                "quality_score": _q["score"],
                "regime": perms.get("regime"),
                "liquidity": round(min(1.0, float(feats.get("tick_rate")
                                                  or 0) / 1.5), 3),
                "spread_pctl": feats.get("spread_pctl"),
                "volatility_pctl": (round(sum(1 for x in _vs if x <= _v_now)
                                          / len(_vs), 3) if _vs else None),
                "version": 1,
            }
            if (verdict == "live_traded" and risk_res.get("ok")
                    and risk_res.get("lot")):
                _mult = scalp_size_multiplier(_q["score"])
                if _mult < 1.0:
                    _scaled = int((risk_res["lot"] * _mult)
                                  / self.cfg.lot_step) * self.cfg.lot_step
                    _scaled = max(self.cfg.min_lot, round(_scaled, 2))
                    if _scaled < risk_res["lot"]:
                        risk_res = {**risk_res, "lot": _scaled,
                                    "quality_size_mult": _mult}
                        doc["lot"] = _scaled
        except Exception:
            logger.exception("trade quality scoring failed (observe-only)")
        _bg(lambda d=dict(doc): db.scalp_decisions.insert_one(dict(d)),
            "decision_insert")
        self._emit(db, "DecisionCreated", decision_id=decision_id,
                   payload={"verdict": verdict, "direction": direction,
                            "net_edge_pips": edge_res["net_edge_pips"],
                            "mode": self.mode})
        if verdict == "live_traded":
            self._emit(db, "RiskApproved", decision_id=decision_id,
                       payload={"lot": risk_res.get("lot"),
                                "stop_pips": fc.stop_pips,
                                "target_pips": fc.target_pips})
        self.last_decision = doc

        self.open_sims.append(ShadowSim(
            decision_id, direction, entry_mid, entry_exec,
            fc.target_pips, fc.stop_pips, self.cfg.pip_size, t.broker_time_ms,
            self.risk_state.limits.max_holding_ms,
            exit_slippage_pips=fc.expected_slippage_pips,
            commission_pips=commission_pips))
        if verdict == "shadow_traded":
            self.counters["shadow_trades"] += 1
        if verdict == "live_traded":
            await self._submit_live(db, doc, fc, risk_res)

    def _emit(self, db, event_type: str, decision_id: str | None = None,
              trade_id: str | None = None, payload: dict | None = None):
        """Append to the immutable trade_events lifecycle stream (non-blocking)."""
        from trade_events import build, append
        ev = build(event_type, user_id=self.user_id, decision_id=decision_id,
                   trade_id=trade_id, account_id=self.account_id,
                   symbol=self.symbol, payload=payload)
        _bg(lambda e=ev: append(db, e), "trade_event")

    async def _submit_live(self, db, decision: dict, fc, risk_res):
        """Order via the bridge queue. Account is PRELOADED (item 5); the
        freshest in-memory quote is rechecked for spread + drift (item 7);
        the EA's entry_price slippage veto is the broker-side last gate."""
        t = self.state.last_tick
        pip = self.cfg.pip_size
        # round 17 item 1 — QUOTE VALIDITY at commitment time: the freshest
        # streamed tick must be recent, two-sided and from THIS runner's
        # account stream (keyed account:symbol by construction); a stalled
        # or one-sided quote refuses the commitment outright. Stale broker
        # symbol specs also refuse (EA stopped refreshing → its clamps and
        # stop constraints can't be trusted at order time).
        nm_now = now_ms()
        from protection_guard import symbol_specs_status
        spec_status = symbol_specs_status(self.account, self.symbol)
        quote_bad = (t is None or t.bid <= 0 or t.ask <= 0 or t.ask < t.bid
                     or nm_now - t.received_time_ms > MAX_SUBMIT_QUOTE_AGE_MS)
        if quote_bad or spec_status == "stale":
            self.state.record_reject()
            why = ("stale symbol specs" if not quote_bad else
                   ("no valid two-sided quote" if t is None or t.bid <= 0
                    or t.ask <= 0 or t.ask < t.bid
                    else f"quote {nm_now - t.received_time_ms}ms old "
                         f"> {MAX_SUBMIT_QUOTE_AGE_MS}ms"))
            _bg(lambda s={"verdict": "rejected",
                          "reject_stage": "pre_submit_quote_invalid",
                          "quote_reason": why,
                          "quote_age_ms": (nm_now - t.received_time_ms
                                           if t else None),
                          "quote_source": f"{self.account_id}:{self.symbol}",
                          "symbol_specs_status": spec_status}:
                db.scalp_decisions.update_one(
                    {"decision_id": decision["decision_id"]}, {"$set": s}),
                "decision_update")
            return
        # ABSOLUTE price-drift guard (round 3 item 7): favorable drift also
        # invalidates the forecast geometry — reject either way, the next
        # evaluation cycle re-forecasts from the new price state.
        ref = decision["sim"]["entry_mid"]
        cur_mid = (t.bid + t.ask) / 2.0
        drift = abs(cur_mid - ref) / pip
        if drift > MAX_DRIFT_BEFORE_SUBMIT_FRAC * fc.stop_pips:
            self.state.record_reject()
            _bg(lambda s={"verdict": "rejected", "reject_stage": "pre_submit_drift",
                          "drift_pips": round(drift, 2)}: db.scalp_decisions.update_one(
                {"decision_id": decision["decision_id"]}, {"$set": s}),
                "decision_update")
            return
        sp = self.state.spread_pips()
        if sp is None or sp > dynamic_spread_limit(self.state, self.cfg):
            self.state.record_reject()
            _bg(lambda s={"verdict": "rejected", "reject_stage": "pre_submit_spread"}: db.scalp_decisions.update_one(
                {"decision_id": decision["decision_id"]}, {"$set": s}),
                "decision_update")
            return
        entry = t.ask if decision["direction"] == "BUY" else t.bid
        # round 16 items 7/8 — FINAL COMMITMENT EDGE: reprice the approved
        # candidate at the freshest quote. Round 17 item 2 — when the market
        # moved MATERIALLY since the decision, a subtractive adjustment is
        # not enough: regenerate the full forecast (fresh features → fresh
        # probability → fresh move/costs/geometry). Small moves keep the
        # lightweight adjustment as a latency optimization.
        decision_spread = float(getattr(fc, "expected_spread_cost_pips", 0) or 0)
        adverse_pips = ((cur_mid - ref) / pip if decision["direction"] == "BUY"
                        else (ref - cur_mid) / pip)
        spread_delta = max(0.0, sp - decision_spread)
        reforecast = None
        if (drift > REQUOTE_DRIFT_FRAC * fc.stop_pips
                or spread_delta > REQUOTE_SPREAD_DELTA_PIPS):
            feats2 = snapshot(self.state)
            cand2 = decision.get("setup")
            fc2 = edge2 = None
            if feats2 is not None and cand2:
                pred2 = scalp_model.predict(self.model_key(), feats2)
                fc2 = make_forecast(feats2, cand2, self.state, self.cfg,
                                    model_p=pred2["p"],
                                    commission_pips=self._commission_pips())
                edge2 = edge.evaluate(fc2)
                reforecast = {"net_edge_pips": edge2["net_edge_pips"],
                              "ok": edge2["ok"],
                              "reason": edge2.get("reason"),
                              "model_source": pred2["source"],
                              "trigger": {"drift_pips": round(drift, 3),
                                          "spread_delta_pips":
                                              round(spread_delta, 3)}}
            if edge2 is None or not edge2["ok"]:
                self.state.record_reject()
                _bg(lambda s={"verdict": "rejected",
                              "reject_stage": "pre_submit_reforecast",
                              "reforecast": reforecast
                              or {"reason": "no fresh feature snapshot"}}:
                    db.scalp_decisions.update_one(
                        {"decision_id": decision["decision_id"]},
                        {"$set": s}), "decision_update")
                return
            fc = fc2                       # fresh geometry drives SL/TP too
            final_net_edge = float(edge2["net_edge_pips"])
        else:
            final_net_edge = (float(decision["net_edge_pips"])
                              - spread_delta
                              - max(0.0, adverse_pips))
        if final_net_edge < edge.MIN_NET_EDGE_PIPS + LATENCY_EDGE_BUFFER_PIPS:
            self.state.record_reject()
            _bg(lambda s={"verdict": "rejected",
                          "reject_stage": "pre_submit_edge_revalidation",
                          "final_net_edge_pips": round(final_net_edge, 3),
                          "reforecast": reforecast,
                          "spread_delta_pips": round(spread_delta, 3),
                          "adverse_drift_pips": round(max(0.0, adverse_pips), 3)}:
                db.scalp_decisions.update_one(
                    {"decision_id": decision["decision_id"]}, {"$set": s}),
                "decision_update")
            return
        # refinement 1 — EXECUTION QUALITY (0-100) immediately before
        # submission from observable conditions; poor conditions GATE the
        # trade even when expected value is positive (user decision).
        # refinement 2 — the min net edge ADAPTS to conditions within
        # [0.15p, 0.60p]: never looser than the historical floor.
        from scalp.exec_quality import (EXEC_QUALITY_MIN, adaptive_min_edge,
                                        blend, execution_quality,
                                        session_name)
        from scalp import broker_stats
        _sp_hist = sorted(self.state.spreads)
        _sp_pctl = (sum(1 for s in _sp_hist if s <= sp) / len(_sp_hist)
                    if _sp_hist else None)
        _feats_eq = snapshot(self.state)
        _vol_ratio = None
        if _feats_eq and _feats_eq.get("vol_long"):
            _vol_ratio = (float(_feats_eq.get("vol_short") or 0)
                          / float(_feats_eq["vol_long"]))
        _ack_avg = (sum(self.ack_ms_recent) / len(self.ack_ms_recent)
                    if self.ack_ms_recent else None)
        # review item 5 — persisted broker/session priors survive restarts;
        # local telemetry is blended in as it accumulates.
        _prior = await broker_stats.summary(db, self.broker)
        _sess = ((_prior.get("sessions") or {})
                 .get(session_name(datetime.now(timezone.utc).hour)) or {})
        _slip_local = (self.state.slippage_ewma_pips
                       if self.state.fills_seen else None)
        eq = execution_quality(
            spread_pctl=_sp_pctl,
            quote_age_ms=now_ms() - t.received_time_ms,
            avg_slippage_pips=blend(_slip_local,
                                    _sess.get("avg_entry_slippage_pips"),
                                    self.state.fills_seen),
            ack_latency_ms=blend(_ack_avg, _sess.get("avg_fill_delay_ms"),
                                 len(self.ack_ms_recent)),
            vol_ratio=_vol_ratio,
            hour_utc=datetime.now(timezone.utc).hour,
            broker_fill_count=(_prior.get("totals") or {}).get("fills") or 0,
            history_cap_exempt=(self.mode == "demo_live"))
        ame = adaptive_min_edge(exec_score=eq["score"], vol_ratio=_vol_ratio,
                                spread_pctl=_sp_pctl,
                                loss_streak=self.risk_state.consecutive_losses)
        _bg(lambda s={"execution_quality": eq, "adaptive_min_edge": ame}:
            db.scalp_decisions.update_one(
                {"decision_id": decision["decision_id"]}, {"$set": s}),
            "decision_update")
        if eq["score"] < EXEC_QUALITY_MIN:
            self.state.record_reject()
            _bg(lambda s={"verdict": "rejected",
                          "reject_stage": "pre_submit_execution_quality",
                          "execution_quality": eq}:
                db.scalp_decisions.update_one(
                    {"decision_id": decision["decision_id"]}, {"$set": s}),
                "decision_update")
            return
        if final_net_edge < ame["min_edge_pips"] + LATENCY_EDGE_BUFFER_PIPS:
            self.state.record_reject()
            _bg(lambda s={"verdict": "rejected",
                          "reject_stage": "pre_submit_adaptive_edge",
                          "final_net_edge_pips": round(final_net_edge, 3),
                          "adaptive_min_edge": ame}:
                db.scalp_decisions.update_one(
                    {"decision_id": decision["decision_id"]}, {"$set": s}),
                "decision_update")
            return
        # round 5 item 8 — attempt-level EV: EV = P(fill)·EV(filled) − C(fail)
        fs = self.fill_stats()
        ev_attempt = (fs["p_fill"] * decision["net_edge_pips"]
                      - (1 - fs["p_fill"]) * FAILED_ATTEMPT_COST_PIPS)
        if fs["attempts"] >= MIN_FILL_ATTEMPTS_FOR_GATE and ev_attempt <= 0:
            self.state.record_reject()
            _bg(lambda s={"verdict": "rejected",
                          "reject_stage": "low_fill_probability_ev",
                          "fill_stats": fs,
                          "ev_attempt_pips": round(ev_attempt, 3)}: db.scalp_decisions.update_one(
                {"decision_id": decision["decision_id"]}, {"$set": s}),
                "decision_update")
            return
        from execution import for_account as engine_for_account
        account = self.account
        if not account:
            return
        engine = engine_for_account(account)
        # round 9 item 4 / round 10 item 1 — re-confirm account ownership at
        # the LAST moment before broker submission with a NON-CACHED read;
        # the cached helper could return stale owned=True after another
        # worker acquired a newer fencing epoch.
        owned, lease_epoch = await confirm_account_lease_now(
            db, self.account_id)
        if not owned:
            self.state.record_reject()
            _bg(lambda: db.scalp_decisions.update_one(
                {"decision_id": decision["decision_id"]},
                {"$set": {"verdict": "rejected",
                          "reject_stage": "lease_lost_before_submit"}}),
                "decision_update")
            return
        # round 14 item 1 — refresh critical broker financial state from the
        # DB immediately before commitment: heartbeats may have moved equity
        # or dropped the connection since the decision snapshot was taken.
        fresh = await db.accounts.find_one(
            {"_id": (account or {}).get("_id")},
            {"equity": 1, "free_margin": 1, "status": 1, "leverage": 1,
             "last_heartbeat": 1, "symbol_specs_updated_at": 1})
        if fresh:
            self.account = {**(self.account or {}), **fresh}
            account = self.account
        stale = broker_state_stale_reason(self.account)
        eq = (self.account or {}).get("equity")
        equity_moved = (eq is not None and self.equity > 0
                        and abs(float(eq) - self.equity) / self.equity > 0.02)
        if stale or equity_moved:
            self.state.record_reject()
            why = stale or (f"equity moved {self.equity:.2f} → "
                            f"{float(eq):.2f} since decision — re-size")
            _bg(lambda s={"verdict": "rejected",
                          "reject_stage": "pre_submit_broker_state",
                          "broker_state_reason": why}: db.scalp_decisions.update_one(
                {"decision_id": decision["decision_id"]}, {"$set": s}),
                "decision_update")
            return
        decision_equity = self.equity
        if eq is not None:
            self.equity = float(eq)
        # round 15 main — RE-RUN sizing on the FINAL accepted snapshot: the
        # lot submitted to the broker must be the lot the fresh equity and
        # current account risk support. Conservative: min(decision lot,
        # freshly sized lot); reject when the fresh checks say no or the
        # smaller lot falls below the broker minimum volume.
        from pip_utils import pip_value_usd_per_lot_strict
        pip_val = pip_value_usd_per_lot_strict(self.symbol)
        if pip_val is None:
            add_account_block(self.account_id, BLOCK_RISK_UNKNOWN)
            self.state.record_reject()
            _bg(lambda: db.scalp_decisions.update_one(
                {"decision_id": decision["decision_id"]},
                {"$set": {"verdict": "rejected",
                          "reject_stage": "pre_submit_risk_unknown"}}),
                "decision_update")
            return
        fresh_risk = risk_check(self.risk_state, self.equity, fc.stop_pips,
                                pip_val, self.cfg)
        final_lot = round(min(float(risk_res["lot"]),
                              float(fresh_risk.get("lot") or 0)), 2)
        acct_check = {"ok": True, "reason": None}
        if fresh_risk["ok"] and final_lot >= self.cfg.min_lot:
            acct_check = check_account(
                self.account_risk, self.equity,
                proposed_stop_risk_usd=final_lot * fc.stop_pips * pip_val)
        margin = margin_audit(self.account, self.symbol, final_lot, entry)
        # round 16 item 3 — FAIL-CLOSED margin: unknown margin blocks live
        # submission unless the broker-native preflight is guaranteed
        # downstream (MT5 OrderSend enforces margin; its rejects persist as
        # attempt_failed). The enforcement basis is always recorded.
        margin["margin_enforcement"] = (
            "backend" if margin["margin_check_passed"] is not None
            else ("broker_native" if BROKER_MARGIN_PREFLIGHT
                  else "unavailable"))
        margin_blocked = (margin["margin_check_passed"] is False
                          or (margin["margin_check_passed"] is None
                              and not BROKER_MARGIN_PREFLIGHT))
        if (not fresh_risk["ok"] or final_lot < self.cfg.min_lot
                or not acct_check["ok"] or margin_blocked):
            self.state.record_reject()
            why = (fresh_risk.get("reason") or acct_check.get("reason")
                   or margin.get("reason")
                   or "fresh sizing below broker minimum volume")
            _bg(lambda s={"verdict": "rejected",
                          "reject_stage": "pre_submit_resize",
                          "resize_reason": why,
                          "fresh_risk": {"ok": fresh_risk["ok"],
                                         "lot": fresh_risk.get("lot"),
                                         "reason": fresh_risk.get("reason")},
                          "margin_audit": margin}: db.scalp_decisions.update_one(
                {"decision_id": decision["decision_id"]}, {"$set": s}),
                "decision_update")
            return
        risk_res = {**risk_res, "lot": final_lot}
        # round 16 item 4 / round 17 item 4 — THREE-WAY EXPOSURE PREFLIGHT:
        # DB (including PENDING/unacked submissions), in-memory account risk
        # and the broker's own reported position count must agree before any
        # new commitment. DB ahead of risk state → fail closed + re-restore;
        # DB claiming more ticketed scalps than the broker reports holding
        # AT ALL → ghost positions, fail closed + re-restore.
        db_open = 0
        unacked = 0
        async for otr in db.trades.find(
                {"account_id": self.account_id, "scope": "scalp_fast",
                 "status": {"$in": ["pending", "open"]}}, {"mt5_ticket": 1}):
            db_open += 1
            if not otr.get("mt5_ticket"):
                unacked += 1
        # review item 2 — uncertain/unqueued provisional reservations are
        # invisible to db.trades: count them as held exposure too.
        from scalp import risk_reservations
        reserved_unaccounted = await risk_reservations.unaccounted_count(
            db, self.account_id)
        broker_pos = (self.account or {}).get("open_positions")
        try:
            broker_pos = int(broker_pos)
        except (TypeError, ValueError):
            broker_pos = None
        ticketed = db_open - unacked
        broker_mismatch = (broker_pos is not None and broker_pos >= 0
                           and ticketed > broker_pos)
        if (db_open + reserved_unaccounted > self.account_risk.open_scalps
                or broker_mismatch):
            self.state.record_reject()
            _bg(lambda s={"verdict": "rejected",
                          "reject_stage": "pre_submit_exposure",
                          "exposure_preflight": {
                              "db_open_scalps": db_open,
                              "unacked_scalps": unacked,
                              "reserved_unaccounted": reserved_unaccounted,
                              "risk_state_open": self.account_risk.open_scalps,
                              "broker_open_positions": broker_pos,
                              "broker_mismatch": broker_mismatch}}:
                db.scalp_decisions.update_one(
                    {"decision_id": decision["decision_id"]}, {"$set": s}),
                "decision_update")
            _bg(lambda: _restore_account_state(db, self.account_id,
                                               force=True), "restore")
            return
        # round 14 item 10 — worker-local guard; round 15 item 3 adds the
        # DISTRIBUTED per-broker slot below. Round 15 item 4: capacity
        # rejections carry their own dataset label so infrastructure
        # saturation is never mistaken for broker/strategy quality.
        global _active_submissions
        if _active_submissions >= MAX_CONCURRENT_SUBMISSIONS:
            self.state.record_reject()
            _bg(lambda: db.scalp_decisions.update_one(
                {"decision_id": decision["decision_id"]},
                {"$set": {"verdict": "rejected",
                          "reject_stage": "submission_capacity",
                          "dataset": "attempt_not_submitted_capacity"}}),
                "decision_update")
            return
        # round 17 item 11 — broken slot↔trade invariants fail NEW entries
        # closed for the broker until a clean lifecycle sweep passes.
        integrity = capacity_integrity_reason(_broker_cap_key(self.broker))
        if integrity is not None:
            self.state.record_reject()
            _bg(lambda r=integrity: db.scalp_decisions.update_one(
                {"decision_id": decision["decision_id"]},
                {"$set": {"verdict": "rejected",
                          "reject_stage": "capacity_integrity",
                          "dataset": "attempt_not_submitted_capacity",
                          "integrity_reason": r}}),
                "decision_update")
            return
        slot = await acquire_broker_submission_slot(
            db, self.broker, self.account_id, decision["decision_id"],
            symbol=self.symbol)
        if slot is None:
            self.state.record_reject()
            _bg(lambda: db.scalp_decisions.update_one(
                {"decision_id": decision["decision_id"]},
                {"$set": {"verdict": "rejected",
                          "reject_stage": "submission_capacity_broker",
                          "dataset": "attempt_not_submitted_capacity"}}),
                "decision_update")
            return
        # round 17 item 6 — snap all order prices to the instrument tick
        # grid (broker point when streamed, else config tick size); generic
        # 5-decimal rounding is wrong off the 5-digit FX grid.
        spec = ((self.account or {}).get("symbol_specs")
                or {}).get(self.symbol) or {}
        tick = float(spec.get("point") or 0) or self.cfg.tick_size
        if decision["direction"] == "BUY":
            sl, tp = entry - fc.stop_pips * pip, entry + fc.target_pips * pip
        else:
            sl, tp = entry + fc.stop_pips * pip, entry - fc.target_pips * pip
        # review item 2 — EA-NATIVE order constraints from the broker's own
        # symbol snapshot: minimum stop distance (stops level), freeze level
        # and symbol trade mode. What the broker will refuse anyway must be
        # refused HERE, before an order intent is created.
        constraint_reason = None
        point = float(spec.get("point") or 0) or self.cfg.tick_size
        min_pts = max(float(spec.get("stops_level_points") or 0),
                      float(spec.get("freeze_level_points") or 0))
        if min_pts > 0:
            sl_pts = abs(entry - sl) / point
            tp_pts = abs(tp - entry) / point
            if min(sl_pts, tp_pts) < min_pts:
                constraint_reason = (
                    f"stop/target inside broker minimum distance "
                    f"({min(sl_pts, tp_pts):.0f} < {min_pts:.0f} points)")
        tm = spec.get("trade_mode")
        if constraint_reason is None and tm is not None:
            try:
                tm = int(tm)
            except (TypeError, ValueError):
                tm = None
            # MT5 SYMBOL_TRADE_MODE: 0=disabled 1=long-only 2=short-only
            # 3=close-only 4=full
            if tm is not None and not (
                    tm == 4
                    or (tm == 1 and decision["direction"] == "BUY")
                    or (tm == 2 and decision["direction"] == "SELL")):
                constraint_reason = (f"broker trade_mode={tm} forbids "
                                     f"{decision['direction']} on {self.symbol}")
        if constraint_reason is not None:
            self.state.record_reject()
            _bg(lambda: release_broker_submission_slot(db, slot),
                "release_submission_slot")
            _bg(lambda s={"verdict": "rejected",
                          "reject_stage": "pre_submit_broker_constraints",
                          "broker_constraint_reason": constraint_reason,
                          "broker_constraints": {
                              "stops_level_points": spec.get("stops_level_points"),
                              "freeze_level_points": spec.get("freeze_level_points"),
                              "trade_mode": spec.get("trade_mode")}}:
                db.scalp_decisions.update_one(
                    {"decision_id": decision["decision_id"]}, {"$set": s}),
                "decision_update")
            return
        submit_ms = now_ms()
        # review item 2 — PROVISIONAL RISK RESERVATION written before the
        # order is queued; held until the broker outcome is certain so a
        # replacement order can't consume the same account risk meanwhile.
        from pip_utils import pip_value_usd_per_lot_strict as _pv_strict
        try:
            _resv = await risk_reservations.reserve(
                db, account_id=self.account_id, user_id=self.user_id,
                decision_id=decision["decision_id"],
                risk_usd=fc.stop_pips * _pv_strict(self.symbol) * final_lot,
                lot=final_lot)
        except Exception:
            # review item 8 — the DB refused (e.g. duplicate ACTIVE
            # reservation for this decision): fail closed, free the slot.
            _bg(lambda: release_broker_submission_slot(db, slot),
                "release_submission_slot")
            self.state.record_reject()
            raise
        self._emit(db, "OrderIntentCreated",
                   decision_id=decision["decision_id"],
                   payload={"lot": final_lot,
                            "entry": round_to_tick(entry, tick),
                            "sl": round_to_tick(sl, tick),
                            "tp": round_to_tick(tp, tick),
                            "slot_id": slot.get("slot_id"),
                            "final_net_edge_pips": round(final_net_edge, 3)})
        _active_submissions += 1
        try:
            trade = await engine.execute(
                user_id=self.user_id, account=account,
                signal={"symbol": self.symbol, "action": decision["direction"],
                        "lot_size": final_lot,
                        "entry_price": round_to_tick(entry, tick),
                        "stop_loss": round_to_tick(sl, tick),
                        "take_profit": round_to_tick(tp, tick),
                        "origin": "auto", "scope": "scalp_fast",
                        "scalp_decision_id": decision["decision_id"],
                        "scalp_lease_epoch": lease_epoch},
                cfg_account_id=self.account_id)
        except BaseException:
            # only a FAILED submission releases here — round 17 main: a
            # queued order keeps its slot leased until the broker acks it
            # or the order reaches a terminal submission state.
            _bg(lambda: release_broker_submission_slot(db, slot),
                "release_submission_slot")
            # round 18 review item 1 — safety-critical transition: AWAITED
            await _resv_transition(db, _resv["reservation_id"], "RELEASED",
                                   release_reason="broker_reject")
            raise
        finally:
            _active_submissions = max(0, _active_submissions - 1)
        # round 15 items 2/9 — persist the COMMITMENT context: the final
        # pre-submit snapshot the order was actually sized against, plus
        # latency decomposition stamps (EA poll latency lives on the trade
        # doc as _dispatched_at; broker ack lands via on_trade_opened).
        _bg(lambda s={
            "pre_submit_account_snapshot": {
                "equity": self.equity,
                "equity_delta": round(self.equity - decision_equity, 2),
                "free_margin": (self.account or {}).get("free_margin"),
                "heartbeat_at": (self.account or {}).get("last_heartbeat"),
                "final_lot": final_lot,
                "lease_epoch": lease_epoch,
                "margin_audit": margin,
            },
            "submitted_lot": final_lot,
            "submission_status": "submitted",
            "final_net_edge_pips": round(final_net_edge, 3),
            "exposure_preflight": {"db_open_scalps": db_open,
                                   "unacked_scalps": unacked},
            "submission_start_ts_ms": submit_ms,
            "db_trade_created_ts_ms": now_ms(),
            "decision_to_submit_ms": max(0, submit_ms
                                         - int(decision.get("ts_ms") or submit_ms)),
        }: db.scalp_decisions.update_one(
            {"decision_id": decision["decision_id"]}, {"$set": s}),
            "decision_update")
        if trade.get("blocked"):
            self.state.record_reject()
            self._emit(db, "BrokerRejected",
                       decision_id=decision["decision_id"],
                       payload={"reason": trade.get("reason")})
            from scalp import broker_stats
            _bg(lambda b=self.broker: broker_stats.record(db, b, rejects=1),
                "broker_stats")
            # round 18 review item 1 — safety-critical transition: AWAITED
            await _resv_transition(db, _resv["reservation_id"], "RELEASED",
                                   release_reason="broker_reject")
            _bg(lambda: release_broker_submission_slot(db, slot),
                "release_submission_slot")
            # round 5 item 8 — rejected attempts are OUTCOMES: they feed the
            # empirical fill-probability model, not just an audit trail.
            _bg(lambda s={"verdict": "rejected", "reject_stage": "broker_blocked",
                          "dataset": "attempt_failed",
                          "submission_result": trade.get("reason"),
                          "block_reason": trade.get("reason")}: db.scalp_decisions.update_one(
                {"decision_id": decision["decision_id"]}, {"$set": s}),
                "decision_update")
            return
        tid = str(trade.get("id") or trade.get("_id") or "")
        # round 18 review item 1 — the stored lifecycle must never lag the
        # real broker lifecycle across a crash: QUEUED_UNCONFIRMED is AWAITED
        # before anything else happens to this submission.
        await _resv_transition(db, _resv["reservation_id"],
                               "QUEUED_UNCONFIRMED", trade_id=tid)
        # round 17 main + review P0 — TRANSFER slot ownership DURABLY. This
        # write is safety-critical: it is awaited, and until it persists we
        # do NOT emit BrokerSubmitted, count open risk, or treat the
        # submission as healthy. On failure the trade is marked uncertain
        # and reconciliation determines the broker outcome; the slot stays
        # leased so sweep_submission_slots can recover it.
        _linked = False
        try:
            _link_res = await db.trades.update_one(
                {"_id": _oid(tid)},
                {"$set": {"submission_slot": dict(slot)}})
            _linked = _link_res.matched_count == 1
        except Exception:
            logger.exception("slot_link write failed trade=%s", tid)
        if not _linked:
            logger.error("slot link NOT persisted trade=%s slot=%s — "
                         "marking submission uncertain", tid,
                         slot.get("slot_id"))
            # review item 2 — the reservation stays HELD (uncertain) until
            # reconciliation determines the broker outcome. AWAITED (r18 i1).
            await _resv_transition(db, _resv["reservation_id"],
                                   "QUEUED_UNCONFIRMED", trade_id=tid,
                                   uncertain=True)
            _bg(lambda: db.trades.update_one(
                {"_id": _oid(tid)},
                {"$set": {"submission_state": "uncertain_slot_link"}}),
                "uncertain_mark")
            _bg(lambda s={"dataset": "submitted_uncertain", "trade_id": tid,
                          "reject_stage": "slot_link_failed",
                          "order_submit_ts_ms": submit_ms}:
                db.scalp_decisions.update_one(
                    {"decision_id": decision["decision_id"]}, {"$set": s}),
                "decision_update")
            return
        self._emit(db, "BrokerSubmitted", decision_id=decision["decision_id"],
                   trade_id=tid, payload={"lot": final_lot})
        # round 18 review item 1 — safety-critical transition: AWAITED
        await _resv_transition(db, _resv["reservation_id"], "SLOT_LINKED")
        # refinement 5 — broker learning: real submission + session spread
        _bg(lambda b=self.broker, s=sp: broker_stats.record(
            db, b, submissions=1, spread_pips=s), "broker_stats")
        self.risk_state.record_open()
        self.account_risk.record_open()
        self._persist_risk(db)
        self.counters["live_trades"] += 1
        # estimated execution cost in USD for the daily cost budget (item 13)
        from pip_utils import pip_value_usd_per_lot_strict
        pv = pip_value_usd_per_lot_strict(self.symbol)
        if pv is None:                    # defensive: evaluate already vetoes
            add_account_block(self.account_id, BLOCK_RISK_UNKNOWN)
            pv = 0.0
        est_cost_usd = ((sp or 0) + 2 * fc.expected_slippage_pips) * pv * risk_res["lot"] \
            + self.commission_usd_per_lot_side * 2 * risk_res["lot"]
        # round 6 item 5 — register this position's monetary stop risk
        stop_risk_usd = risk_res["lot"] * fc.stop_pips * pv
        self.account_risk.add_stop_risk(tid, stop_risk_usd)
        # round 18 review item 4 — lifecycle: QUEUED until the broker's fill
        # ack (/bridge/report status=open) arrives. MT5 market orders carry
        # SL/TP with the order, so that single fill ack collapses
        # BROKER_ACCEPTED / FILLED / PROTECTED into OPEN; the EA claim time
        # is visible on the trade doc as _dispatched_at. Exit management and
        # the max-holding clock only start once the broker confirms.
        self.live_trades[tid] = {
            "state": "QUEUED",
            "queued_ms": now_ms(),
            "opened_ms": now_ms(), "direction": decision["direction"],
            "lot": risk_res["lot"],
            "entry_px": entry, "stop_px": sl, "target_px": tp,
            "stop_risk_usd": round(stop_risk_usd, 2),
            "decision_id": decision["decision_id"],
            "order_submit_ts_ms": submit_ms,
            "est_cost_usd": round(est_cost_usd, 2),
            "submission_slot": dict(slot),
        }
        _bg(lambda s={"dataset": "submitted", "trade_id": tid,
                      "order_submit_ts_ms": submit_ms}: db.scalp_decisions.update_one(
            {"decision_id": decision["decision_id"]}, {"$set": s}),
            "decision_update")

    # ---------------- exits & sims ----------------

    def _advance_sims(self, db, t: TickEvent):
        still = []
        for sim in self.open_sims:
            out = sim.advance(t)
            if out is None:
                still.append(sim)
                continue
            self._resolutions_since_retrain += 1
            _bg(lambda d=sim.decision_id, o=out: db.scalp_decisions.update_one(
                {"decision_id": d}, {"$set": {"outcome": o}}), "sim_outcome")
        self.open_sims = still
        if self._resolutions_since_retrain >= RETRAIN_EVERY_RESOLUTIONS:
            self._resolutions_since_retrain = 0
            _bg(lambda: scalp_model.retrain(db, self.symbol,
                                            broker=self.broker,
                                            account_type=self.account_type),
                "model_retrain")

    def _mark_close_requested(self, info: dict, nm: int, reason: str):
        """review item 5 — single place that flips a position to
        CLOSE_REQUESTED and arms the durable-persist retry loop."""
        info["state"] = "CLOSE_REQUESTED"
        info["close_requested_ms"] = nm
        info["close_attempt_ms"] = nm
        info["close_reason_pending"] = reason

    def _monitor_live_exits(self, db, t: TickEvent):
        """Item 14 — positions stay monitored through CLOSE_REQUESTED until
        the broker confirms the close via /bridge/report."""
        if not self.live_trades:
            return
        nm = now_ms()
        sp = self.state.spread_pips() or 0.0
        for tid, info in self.live_trades.items():
            st = info.get("state")
            if st == "QUEUED":
                # review item 4 failsafe — a queued order the broker never
                # confirmed within the max holding window gets a close
                # request; deal reconciliation resolves the true outcome.
                if nm - info.get("queued_ms", info["opened_ms"]) \
                        >= self.risk_state.limits.max_holding_ms:
                    self._mark_close_requested(info, nm, "queued_timeout")
                    _bg(lambda t=tid: self._request_close(
                        db, t, "queued_timeout"), "request_close")
                continue
            if st == "CLOSE_REQUESTED":
                # review item 5 — the close INTENT must become durable: the
                # DB write is retried every 3s until it is confirmed.
                if (not info.get("close_persisted")
                        and nm - info.get("close_attempt_ms", 0) >= 3000):
                    info["close_attempt_ms"] = nm
                    rs = info.get("close_reason_pending") or "close_retry"
                    _bg(lambda t=tid, r=rs: self._request_close(db, t, r),
                        "request_close_retry")
                continue
            if st != "OPEN":
                continue
            reason = None
            if nm - info["opened_ms"] >= self.risk_state.limits.max_holding_ms:
                reason = "max_holding_time"
            elif sp >= 3.0 * self.cfg.max_spread_pips:
                reason = "spread_shock"
            elif self.health["status"] != "OK" and not self.health["open_allowed"]:
                reason = "execution_degraded"
            if reason:
                self._mark_close_requested(info, nm, reason)
                # round 5 item 9 — exact exit-slippage baseline captured at
                # close request: reference quotes + requested exit price.
                lt = self.state.last_tick
                if lt is not None:
                    info["exit_reference_bid"] = lt.bid
                    info["exit_reference_ask"] = lt.ask
                    info["requested_exit_price"] = (
                        lt.bid if info.get("direction") == "BUY" else lt.ask)
                _bg(lambda t=tid, rs=reason: self._request_close(db, t, rs),
                    "request_close")
                continue
            # Batch C — adaptive per-second re-scoring (fixed safety envelope)
            self._adaptive_manage(db, tid, info, nm, sp)

    def _adaptive_manage(self, db, tid: str, info: dict, nm: int, sp: float):
        """Per-second re-scoring of one open scalp. Tighten-only envelope:
        never widens the stop, never touches lots, exits only reduce risk.
        Round 18 review item 2 — the CONFIRMED stop (broker-acknowledged) is
        the only stop the logic trusts; a requested tighten lives in
        pending_stop_px until /bridge/modification-ack confirms it."""
        if nm < info.get("adaptive_next_ms", 0):
            return
        info["adaptive_next_ms"] = nm + adaptive_exits.EVAL_INTERVAL_MS
        lt = self.state.last_tick
        if lt is None:
            return
        mid = (lt.bid + lt.ask) / 2.0
        d = info.get("direction", "BUY")
        sign = 1.0 if d == "BUY" else -1.0
        # review item 7 — max favorable/adverse excursion in R (vs the
        # ORIGINAL stop distance) tracked live for the exit probability
        stop_dist = abs(info["entry_px"] - info["stop_px"]) or self.cfg.pip_size
        exc_r = sign * (mid - info["entry_px"]) / stop_dist
        info["mfe_r"] = max(info.get("mfe_r", 0.0), exc_r)
        info["mae_r"] = max(info.get("mae_r", 0.0), -exc_r)
        feats = snapshot(self.state)
        p_model = None
        vol_ratio = None
        vol_short = None
        if feats:
            p_model = scalp_model.predict_p(self.model_key(), feats)
            vol_short = float(feats.get("vol_short") or 0) or None
            if feats.get("vol_long"):
                vol_ratio = (float(feats.get("vol_short") or 0)
                             / float(feats["vol_long"]))
        perms = permissions.get_cached(self.user_id, self.symbol)
        regime_opposes = bool(
            (d == "BUY" and not perms.get("long_enabled")
             and perms.get("short_enabled"))
            or (d == "SELL" and not perms.get("short_enabled")
                and perms.get("long_enabled")))
        cur_sl = info.get("confirmed_stop_px") or info["stop_px"]
        # review item 7 — position-conditioned probability: P(THIS target
        # before THIS stop within the REMAINING horizon). The fresh-entry
        # model score only supplies the drift tilt.
        p_pos = adaptive_exits.position_p_target(
            direction=d, entry_px=info["entry_px"], mid=mid,
            stop_px=cur_sl, target_px=info["target_px"],
            pip_size=self.cfg.pip_size, p_model=p_model,
            elapsed_ms=nm - info["opened_ms"],
            max_holding_ms=self.risk_state.limits.max_holding_ms,
            vol_short_pips=vol_short, spread_pips=sp,
            mfe_frac=info.get("mfe_r"), mae_frac=info.get("mae_r"))
        act = adaptive_exits.evaluate(
            direction=d, entry_px=info["entry_px"], mid=mid,
            stop_px=cur_sl,
            target_px=info["target_px"], pip_size=self.cfg.pip_size,
            elapsed_ms=nm - info["opened_ms"],
            max_holding_ms=self.risk_state.limits.max_holding_ms,
            p_target=p_pos, regime_opposes=regime_opposes, vol_ratio=vol_ratio,
            spread_pips=sp, spread_limit=self.cfg.max_spread_pips)
        if act["action"] == "EXIT_NOW":
            act["p_model"] = p_model
            act["p_position"] = p_pos
            self._mark_close_requested(info, nm, act["reason"])
            info["exit_reference_bid"] = lt.bid
            info["exit_reference_ask"] = lt.ask
            info["requested_exit_price"] = lt.bid if d == "BUY" else lt.ask
            info["adaptive_exit"] = act
            _bg(lambda t=tid, rs=act["reason"]: self._request_close(db, t, rs),
                "request_close")
            return
        if act["action"] == "TIGHTEN_STOP":
            if nm < info.get("adaptive_tighten_next_ms", 0):
                return
            # review item 2 — one in-flight stop modification at a time; an
            # unacknowledged request expires after 30s and may be retried.
            if info.get("pending_stop_px") is not None:
                if nm - info.get("pending_stop_ms", 0) < 30_000:
                    return
                info.pop("pending_stop_px", None)
                info.pop("pending_stop_request_id", None)
            new_sl = adaptive_exits.clamp_tighter(
                d, cur_sl, act["proposed_stop_px"], mid, self.cfg.pip_size)
            if new_sl is None:                      # envelope: only tighter
                return
            # review item 3 — snap to the instrument tick grid (broker point
            # when streamed); a fixed 5-decimal round is wrong for
            # XAU/JPY/indices/crypto tick sizes.
            spec = ((self.account or {}).get("symbol_specs")
                    or {}).get(self.symbol) or {}
            tick = float(spec.get("point") or 0) or self.cfg.tick_size
            new_sl = round_to_tick(new_sl, tick)
            if (d == "BUY" and new_sl <= cur_sl) or \
                    (d == "SELL" and new_sl >= cur_sl):
                return                              # rounding ate the gain
            req_id = uuid.uuid4().hex
            info["adaptive_tighten_next_ms"] = (
                nm + adaptive_exits.TIGHTEN_COOLDOWN_MS)
            info["pending_stop_px"] = new_sl
            info["pending_stop_ms"] = nm
            info["pending_stop_request_id"] = req_id
            _bg(lambda t=tid, s=new_sl, rs=act["reason"], rq=req_id:
                db.trades.update_one(
                    {"_id": _oid(t), "status": "open"},
                    {"$set": {"pending_modification": {
                        "type": "MODIFY_SL", "new_sl": s,
                        "request_id": rq,
                        "requested_at": datetime.now(
                            timezone.utc).isoformat(),
                        "reason": rs}},
                     "$push": {"adaptive_actions": {
                         "ts_ms": nm, "action": "TIGHTEN_STOP",
                         "reason": rs, "new_sl": s,
                         "request_id": rq}}}),
                "adaptive_tighten")

    def on_stop_modified(self, trade_id: str, new_sl: float | None,
                         success: bool, db=None):
        """Round 18 review item 2 — EA/broker acknowledgement of a MODIFY_SL:
        only now does the tighter stop become the CONFIRMED stop. A rejected
        modification leaves the previous confirmed stop in force."""
        info = self.live_trades.get(trade_id)
        if info is None:
            return
        requested = info.pop("pending_stop_px", None)
        req_id = info.pop("pending_stop_request_id", None)
        info.pop("pending_stop_ms", None)
        if success and new_sl:
            info["confirmed_stop_px"] = float(new_sl)
            if db is not None:
                self._emit(db, "StopModifyConfirmed", trade_id=trade_id,
                           decision_id=info.get("decision_id"),
                           payload={"new_sl": float(new_sl),
                                    "request_id": req_id})
        elif db is not None:
            self._emit(db, "StopModifyRejected", trade_id=trade_id,
                       decision_id=info.get("decision_id"),
                       payload={"requested_sl": requested,
                                "request_id": req_id})

    async def _request_close(self, db, trade_id: str, reason: str):
        res = await db.trades.update_one(
            {"_id": _oid(trade_id), "status": "open"},
            {"$set": {"pending_modification": {
                "type": "FULL_CLOSE",
                "requested_at": datetime.now(timezone.utc).isoformat(),
                "reason": f"scalp_{reason}"},
                "close_reason": f"scalp_{reason}"}})
        # review item 5 — the close intent is DURABLE only once this write
        # returns; until then the exit monitor keeps retrying it.
        info = self.live_trades.get(trade_id)
        if info is not None:
            info["close_persisted"] = True
            if res.matched_count == 0:
                info["close_persist_note"] = "trade_not_open_in_db"

    def _release_submission_slot_of(self, info: dict | None, db,
                                     trade_id: str = ""):
        """Round 17 main — free the trade's submission slot at broker
        acknowledgement / terminal state. Token-fenced + idempotent; when
        db is unavailable the lifecycle sweep releases it instead."""
        slot = (info or {}).pop("submission_slot", None)
        if slot and db is not None:
            _bg(lambda: release_broker_submission_slot(db, slot),
                "release_submission_slot")
            if trade_id:
                _bg(lambda: db.trades.update_one(
                    {"_id": _oid(trade_id)},
                    {"$unset": {"submission_slot": ""}}), "slot_unlink")

    def on_trade_opened(self, trade_id: str, requested_price: float | None,
                        actual_price: float | None, db=None):
        """Broker fill confirmation (round 3 item 5): feed REAL entry slippage
        back into the state so the cost model learns from live fills."""
        info = self.live_trades.get(trade_id)
        self.last_order_ack_ms = now_ms()
        # round 18 review item 4 — the broker fill ack is the lifecycle
        # gate: QUEUED → OPEN, and the holding clock starts HERE.
        if info is not None and info.get("state") == "QUEUED":
            info["state"] = "OPEN"
            info["broker_ack_ms"] = self.last_order_ack_ms
            info["opened_ms"] = self.last_order_ack_ms
        self._release_submission_slot_of(info, db, trade_id)
        if db is not None:
            _bg(lambda t=trade_id: risk_reservations.release_for_trade(
                db, t, "broker_ack"), "resv_release")
        if db is not None:
            self._emit(db, "PositionOpened", trade_id=trade_id,
                       decision_id=(info or {}).get("decision_id"),
                       payload={"requested_price": requested_price,
                                "actual_price": actual_price})
        if info is not None and not info.get("fill_counted"):
            info["fill_counted"] = True
            self.exec_fills += 1
        if requested_price and actual_price:
            direction = (info or {}).get("direction", "BUY")
            signed = ((actual_price - requested_price) if direction == "BUY"
                      else (requested_price - actual_price)) / self.cfg.pip_size
            self.state.record_fill(signed)
            if info is not None:
                info["requested_entry"] = requested_price
                info["actual_entry"] = actual_price
                info["entry_slippage_pips"] = round(signed, 2)
            if db is not None and info:
                ack_ms = self.last_order_ack_ms
                sub_ms = info.get("order_submit_ts_ms")
                if sub_ms:
                    self.ack_ms_recent.append(max(0, ack_ms - int(sub_ms)))
                from scalp import broker_stats
                _bg(lambda b=self.broker, sl=signed,
                    a=(max(0, ack_ms - int(sub_ms)) if sub_ms else None):
                    broker_stats.record(db, b, entry_slip_pips=sl, ack_ms=a),
                    "broker_stats")
                _bg(lambda: db.scalp_decisions.update_one(
                    {"decision_id": info.get("decision_id", "")},
                    {"$set": {"requested_entry": requested_price,
                              "actual_entry": actual_price,
                              "entry_slippage_pips": round(signed, 2),
                              "submission_status": "filled",
                              # round 15 item 9 — latency decomposition
                              "broker_ack_ts_ms": ack_ms,
                              "submit_to_ack_ms": (max(0, ack_ms - int(sub_ms))
                                                   if sub_ms else None)}}),
                    "entry_fill")

    def on_close_ack(self, trade_id: str, exit_price: float | None = None,
                     db=None):
        """Operational close acknowledgement (/bridge/report). Frees the
        position slot and stops exit monitoring — but applies NO financials.
        /bridge/external-deal is the authoritative reconciliation source
        (round 5 item 1); the info dict is parked until its deal arrives."""
        if trade_id in self._financials_applied:
            return
        info = self.live_trades.pop(trade_id, None)
        if info is None:
            return
        self._release_submission_slot_of(info, db, trade_id)
        info["close_ack_ms"] = now_ms()
        if exit_price:
            info["ack_exit_price"] = exit_price
        self._closed_awaiting_financials[trade_id] = info
        if len(self._closed_awaiting_financials) > 200:
            self._closed_awaiting_financials.pop(
                next(iter(self._closed_awaiting_financials)))
        self.risk_state.record_close()
        self.account_risk.record_close()
        self.account_risk.remove_stop_risk(trade_id)
        if db is not None:
            self._persist_risk(db)

    def on_trade_closed(self, trade_id: str, pnl: float,
                        commission: float = 0.0, swap: float = 0.0,
                        exit_price: float | None = None,
                        deal_id: str | None = None,
                        close_reason: str | None = None,
                        source: str = "report", db=None):
        """Authoritative financial reconciliation (round 5 items 1/2) —
        normally fed by /bridge/external-deal with SIGNED broker figures.
        Idempotent per deal_id AND per trade_id: financials apply exactly
        once even when both reporting paths fire.

        SIGNED MT5 semantics: net = profit + commission + swap. abs() would
        erase positive-swap credits and commission rebates."""
        if deal_id is not None and deal_id in self._applied_deal_ids:
            return
        if trade_id in self._financials_applied:
            return
        info = self.live_trades.pop(trade_id, None)
        if info is not None:
            self._release_submission_slot_of(info, db, trade_id)
            self.risk_state.record_close()
            self.account_risk.record_close()
        else:
            info = self._closed_awaiting_financials.pop(trade_id, None)
        self.account_risk.remove_stop_risk(trade_id)
        if deal_id is not None:
            self._applied_deal_ids.append(deal_id)
        self._financials_applied.append(trade_id)
        pnl = float(pnl or 0)
        commission = float(commission or 0)
        swap = float(swap or 0)
        net_pnl = pnl + commission + swap
        # round 6 item 9 — loss streak uses TRADING P&L: market result plus
        # commission plus negative financing, EXCLUDING financing credits.
        trading_pnl = pnl + commission + min(0.0, swap)
        execution_cost = max(0.0, -commission) + max(0.0, -swap)
        financing_credit = max(0.0, swap)
        est_cost = float(info.get("est_cost_usd", 0.0)) if info else 0.0
        broker_costs_known = (commission != 0.0 or swap != 0.0
                              or source == "broker_deal")
        cost_used = execution_cost if broker_costs_known else est_cost
        self.risk_state.record_result(net_pnl, cost_used,
                                      trading_pnl_usd=trading_pnl)
        self.account_risk.record_result(net_pnl, cost_used,
                                        trading_pnl_usd=trading_pnl)
        # round 6 item 8 — exit-slippage baseline: a CONFIRMED stop-loss exit
        # ALWAYS measures against the stored stop price (gap-through-stop is
        # exactly where slippage matters most). Otherwise use the close-request
        # quote, falling back to the stop level only for near-stop fills.
        exit_slip = None
        req_exit = (info or {}).get("requested_exit_price")
        is_stop = bool(close_reason and "stop_loss" in str(close_reason))
        if info and info.get("stop_px") and (is_stop or (
                req_exit is None and exit_price
                and abs(exit_price - float(info["stop_px"])) <= 3 * self.cfg.pip_size)):
            req_exit = float(info["stop_px"])
        if info and exit_price and req_exit:
            d = info.get("direction", "BUY")
            adverse = ((req_exit - exit_price) if d == "BUY"
                       else (exit_price - req_exit)) / self.cfg.pip_size
            exit_slip = round(adverse, 2)
        if db is not None and exit_slip is not None:
            from scalp import broker_stats
            _bg(lambda b=self.broker, s=exit_slip: broker_stats.record(
                db, b, exit_slip_pips=s), "broker_stats")
        if db is not None:
            self._persist_risk(db)
            # round 8 item 3 — immutable financial-event ledger (replayable
            # audit trail; risk snapshots stay the fast-path cache)
            ev = {"account_id": self.account_id, "symbol": self.symbol,
                  "trade_id": trade_id, "deal_id": deal_id,
                  "event_type": "full_close",
                  "net_pnl": round(net_pnl, 2),
                  "trading_pnl": round(trading_pnl, 2),
                  "execution_cost": round(cost_used, 2),
                  "commission": commission, "swap": swap,
                  "risk_applied": True, "lease_epoch": _lease_epoch.get(self.account_id, 0),
                  "at": datetime.now(timezone.utc).isoformat()}
            self._last_financial_event = ev
            _bg(lambda e=ev: db.scalp_financial_events.update_one(
                {"account_id": e["account_id"], "deal_id": e["deal_id"],
                 "event_type": e["event_type"]},
                {"$setOnInsert": e}, upsert=True), "financial_event")
            self._emit(db, "PositionClosed", trade_id=trade_id,
                       decision_id=(info or {}).get("decision_id"),
                       payload={"net_pnl_usd": round(net_pnl, 2),
                                "close_reason": close_reason,
                                "exit_price": exit_price})
            _bg(lambda t=trade_id: risk_reservations.release_for_trade(
                db, t, "closed"), "resv_release")
            self._emit(db, "FinancialApplied", trade_id=trade_id,
                       decision_id=(info or {}).get("decision_id"),
                       payload={"deal_id": deal_id,
                                "net_pnl_usd": round(net_pnl, 2),
                                "commission_usd": commission,
                                "swap_usd": swap, "source": source})
            if info:
                outcome = {
                    "requested_entry_price": info.get("requested_entry"),
                    "actual_entry_price": info.get("actual_entry", info.get("entry_px")),
                    "entry_slippage_pips": info.get("entry_slippage_pips"),
                    "requested_exit_price": req_exit,
                    "actual_exit_price": exit_price,
                    "exit_reference_bid": info.get("exit_reference_bid"),
                    "exit_reference_ask": info.get("exit_reference_ask"),
                    "exit_slippage_pips": exit_slip,
                    "exit_reason": close_reason,
                    "close_request_ts_ms": info.get("close_requested_ms"),
                    "gross_market_pnl_usd": pnl,
                    "commission_usd": commission,          # SIGNED
                    "swap_usd": swap,                      # SIGNED
                    "net_pnl_usd": round(net_pnl, 2),
                    "trading_pnl_usd": round(trading_pnl, 2),
                    "execution_cost_usd": round(execution_cost, 2),
                    "financing_credit_usd": round(financing_credit, 2),
                    "execution_cost_usd_used": round(cost_used, 2),
                    "cost_source": "broker" if broker_costs_known else "estimated",
                    "broker_deal_id": deal_id,
                }
                _bg(lambda o=outcome: db.scalp_decisions.update_one(
                    {"decision_id": info.get("decision_id", "")},
                    {"$set": {"dataset": "filled_live",
                              "execution_outcome": o,
                              "live_pnl_usd": o["net_pnl_usd"],
                              "live_cost_usd": o["execution_cost_usd_used"]}}),
                    "live_close")

    def on_partial_close(self, trade_id: str, closed_lots: float,
                         remaining_lots: float, pnl: float,
                         commission: float = 0.0, swap: float = 0.0,
                         exit_price: float | None = None,
                         deal_id: str | None = None, db=None):
        """Round 6 item 1 — partial closes hit the SAME financial budgets as
        full closes: realized P&L, signed costs, streak (trading P&L), and
        the tracked lot/stop-risk shrink. The position slot stays OPEN —
        record_close() only fires when the final volume reaches zero."""
        if deal_id is not None and deal_id in self._applied_deal_ids:
            return
        if trade_id in self._financials_applied:
            return
        if deal_id is not None:
            self._applied_deal_ids.append(deal_id)
        pnl = float(pnl or 0)
        commission = float(commission or 0)
        swap = float(swap or 0)
        net_pnl = pnl + commission + swap
        trading_pnl = pnl + commission + min(0.0, swap)
        execution_cost = max(0.0, -commission) + max(0.0, -swap)
        self.risk_state.record_result(net_pnl, execution_cost,
                                      trading_pnl_usd=trading_pnl)
        self.account_risk.record_result(net_pnl, execution_cost,
                                        trading_pnl_usd=trading_pnl)
        info = self.live_trades.get(trade_id)
        if info is not None:
            prior_lot = float(info.get("lot") or 0)
            info["lot"] = max(0.0, float(remaining_lots or 0))
            # round 8 item 6 — EXACT remaining stop risk from the broker's
            # remaining volume and the stored stop, not proportional scaling
            entry, stop = info.get("entry_px"), info.get("stop_px")
            if info["lot"] > 0 and entry and stop:
                from pip_utils import pip_value_usd_per_lot_strict
                pv = pip_value_usd_per_lot_strict(self.symbol)
                if pv is None:
                    # round 11 item 6 — unpriceable: risk UNKNOWN, block
                    add_account_block(self.account_id, BLOCK_RISK_UNKNOWN)
                    if prior_lot > 0:
                        self.account_risk.scale_stop_risk(
                            trade_id, info["lot"] / prior_lot)
                else:
                    self.account_risk.add_stop_risk(
                        trade_id, info["lot"] * abs(float(entry) - float(stop))
                        / self.cfg.pip_size * pv)
            elif prior_lot > 0 and info["lot"] > 0:
                self.account_risk.scale_stop_risk(trade_id,
                                                  info["lot"] / prior_lot)
            elif info["lot"] <= 0:
                self.account_risk.remove_stop_risk(trade_id)
            info.setdefault("partial_exits", []).append({
                "deal_id": deal_id, "closed_lots": float(closed_lots or 0),
                "exit_price": exit_price, "net_pnl_usd": round(net_pnl, 2),
                "ts_ms": now_ms()})
        if db is not None:
            self._persist_risk(db)
            ev = {"account_id": self.account_id, "symbol": self.symbol,
                  "trade_id": trade_id, "deal_id": deal_id,
                  "event_type": "partial_close",
                  "net_pnl": round(net_pnl, 2),
                  "trading_pnl": round(trading_pnl, 2),
                  "execution_cost": round(execution_cost, 2),
                  "commission": commission, "swap": swap,
                  "remaining_lots": float(remaining_lots or 0),
                  "risk_applied": True, "status": "applied",
                  "lease_epoch": _lease_epoch.get(self.account_id, 0),
                  "at": datetime.now(timezone.utc).isoformat()}
            self._last_financial_event = ev
            _bg(lambda e=ev: db.scalp_financial_events.update_one(
                {"account_id": e["account_id"], "deal_id": e["deal_id"],
                 "event_type": e["event_type"]},
                {"$setOnInsert": e}, upsert=True), "financial_event")
            self._emit(db, "FinancialApplied", trade_id=trade_id,
                       decision_id=(info or {}).get("decision_id"),
                       payload={"deal_id": deal_id,
                                "net_pnl_usd": round(net_pnl, 2),
                                "commission_usd": commission,
                                "swap_usd": swap,
                                "partial": True,
                                "remaining_lots": float(remaining_lots or 0)})
            if info and info.get("decision_id"):
                rec = {"deal_id": deal_id,
                       "closed_lots": float(closed_lots or 0),
                       "remaining_lots": float(remaining_lots or 0),
                       "exit_price": exit_price,
                       "gross_market_pnl_usd": pnl,
                       "commission_usd": commission, "swap_usd": swap,
                       "net_pnl_usd": round(net_pnl, 2),
                       "at": datetime.now(timezone.utc).isoformat()}
                _bg(lambda r=rec: db.scalp_decisions.update_one(
                    {"decision_id": info["decision_id"]},
                    {"$push": {"partial_exit_records": r}}), "partial_close")

    # ---------------- risk-state persistence (item 9) ----------------

    async def persist_risk_now(self, db):
        """Round 7 item 6 — SYNCHRONOUS risk + deal-id persistence, awaited
        BEFORE a broker deal may be marked reconciliation-complete.
        Round 8 item 2 — epoch-fenced: a stale worker (older lease_epoch)
        cannot overwrite state persisted by the current owner."""
        now_iso = datetime.now(timezone.utc).isoformat()
        epoch = _lease_epoch.get(self.account_id, 0)
        fence = {"$or": [{"lease_epoch": {"$exists": False}},
                         {"lease_epoch": {"$lte": epoch}}]}
        res = await db.scalp_risk_state.update_one(
            {"account_id": self.account_id, "symbol": self.symbol, **fence},
            {"$set": {"user_id": self.user_id, **self.risk_state.to_doc(),
                      "applied_deal_ids": list(self._applied_deal_ids),
                      "lease_epoch": epoch, "saved_at": now_iso}}, upsert=False)
        if not res.matched_count:
            existing = await db.scalp_risk_state.find_one(
                {"account_id": self.account_id, "symbol": self.symbol},
                {"lease_epoch": 1})
            # Round 10 fix (iter-45) — stale ONLY when the existing doc holds
            # a strictly NEWER epoch. A doc with our own (or older) epoch
            # means we lost an insert race against our own background
            # _persist_risk between update_one and find_one: retry the
            # fenced write instead of falsely failing the reconciliation.
            if existing is not None and int(existing.get("lease_epoch") or 0) > epoch:
                raise RuntimeError(
                    f"risk persist fenced out (stale lease epoch {epoch} < "
                    f"{existing.get('lease_epoch')})")
            if existing is not None:
                await db.scalp_risk_state.update_one(
                    {"account_id": self.account_id, "symbol": self.symbol,
                     **fence},
                    {"$set": {"user_id": self.user_id,
                              **self.risk_state.to_doc(),
                              "applied_deal_ids": list(self._applied_deal_ids),
                              "lease_epoch": epoch, "saved_at": now_iso}},
                    upsert=False)
            else:
                await db.scalp_risk_state.update_one(
                    {"account_id": self.account_id, "symbol": self.symbol},
                    {"$setOnInsert": {"user_id": self.user_id,
                                      **self.risk_state.to_doc(),
                                      "applied_deal_ids": list(self._applied_deal_ids),
                                      "lease_epoch": epoch, "saved_at": now_iso}},
                    upsert=True)
        await self._fenced_account_write(db, epoch, now_iso)

    async def _fenced_account_write(self, db, epoch: int, now_iso: str):
        """Round 9 critical — the shared _ACCOUNT doc is fenced too."""
        fence = {"$or": [{"lease_epoch": {"$exists": False}},
                         {"lease_epoch": {"$lte": epoch}}]}
        res = await db.scalp_risk_state.update_one(
            {"account_id": self.account_id, "symbol": "_ACCOUNT", **fence},
            {"$set": {"user_id": self.user_id, **self.account_risk.to_doc(),
                      "lease_epoch": epoch, "saved_at": now_iso}},
            upsert=False)
        if not res.matched_count:
            existing = await db.scalp_risk_state.find_one(
                {"account_id": self.account_id, "symbol": "_ACCOUNT"},
                {"lease_epoch": 1})
            # Round 10 fix (iter-45) — same epoch-aware rejection as
            # persist_risk_now: only a strictly newer epoch is stale.
            if existing is not None and int(existing.get("lease_epoch") or 0) > epoch:
                raise RuntimeError(
                    f"account risk persist fenced out (stale epoch {epoch} < "
                    f"{existing.get('lease_epoch')})")
            if existing is not None:
                await db.scalp_risk_state.update_one(
                    {"account_id": self.account_id, "symbol": "_ACCOUNT",
                     **fence},
                    {"$set": {"user_id": self.user_id,
                              **self.account_risk.to_doc(),
                              "lease_epoch": epoch, "saved_at": now_iso}},
                    upsert=False)
            else:
                await db.scalp_risk_state.update_one(
                    {"account_id": self.account_id, "symbol": "_ACCOUNT"},
                    {"$setOnInsert": {"user_id": self.user_id,
                                      **self.account_risk.to_doc(),
                                      "lease_epoch": epoch, "saved_at": now_iso}},
                    upsert=True)

    def _persist_risk(self, db):
        """Async telemetry mirror of the risk snapshots — FENCED like the
        synchronous path (round 9): a stale-epoch worker's background write
        silently loses instead of clobbering the owner's state."""
        epoch = _lease_epoch.get(self.account_id, 0)
        fence = {"$or": [{"lease_epoch": {"$exists": False}},
                         {"lease_epoch": {"$lte": epoch}}]}
        _bg(lambda: db.scalp_risk_state.update_one(
            {"account_id": self.account_id, "symbol": self.symbol, **fence},
            {"$set": {"user_id": self.user_id, **self.risk_state.to_doc(),
                      "applied_deal_ids": list(self._applied_deal_ids),
                      "lease_epoch": epoch,
                      "saved_at": datetime.now(timezone.utc).isoformat()}},
            upsert=True), "risk_state")
        _bg(lambda: db.scalp_risk_state.update_one(
            {"account_id": self.account_id, "symbol": "_ACCOUNT", **fence},
            {"$set": {"user_id": self.user_id, **self.account_risk.to_doc(),
                      "lease_epoch": epoch,
                      "saved_at": datetime.now(timezone.utc).isoformat()}},
            upsert=True), "account_risk_state")

    async def restore_risk(self, db):
        doc = await db.scalp_risk_state.find_one(
            {"account_id": self.account_id, "symbol": self.symbol})
        if doc:
            self.risk_state.load_doc(doc)
            # round 6 item 6 — durable deal-id idempotency cache survives
            # restarts; the broker_deals reconciliation status is authoritative
            for did in (doc.get("applied_deal_ids") or [])[-500:]:
                self._applied_deal_ids.append(did)
        # round 7 item 5 — account-level state (risk doc, stop risk, open
        # count) initializes ONCE per account, not per symbol runner
        await _restore_account_state(db, self.account_id)
        # round 5 item 5 — reconstruct DETAILED open-position state so a
        # restarted process can still run max-holding / spread-shock exits
        # and attribute exit slippage correctly.
        # round 13 items 1/2 — FULL cursor (no .to_list truncation) and
        # STRICT symbol scoping: this runner tracks ONLY its own symbol's
        # open scalp trades; account-wide state lives in _ACCOUNT via
        # _restore_account_state.
        from pip_utils import base_symbol
        open_count = 0
        async for tr in db.trades.find(
                {"account_id": self.account_id, "scope": "scalp_fast",
                 "status": "open"}):
            if base_symbol(tr.get("symbol") or "") != self.symbol:
                continue
            open_count += 1
            tid = str(tr["_id"])
            if tid in self.live_trades:
                continue
            pend = tr.get("pending_modification") or {}
            info = {
                "state": "CLOSE_REQUESTED" if pend.get("type") == "FULL_CLOSE" else "OPEN",
                "opened_ms": _iso_to_ms(tr.get("opened_at")) or now_ms(),
                "direction": tr.get("action"),
                "lot": float(tr.get("lot_size") or 0),
                "entry_px": tr.get("entry_price"),
                "requested_entry": tr.get("requested_price") or tr.get("intended_entry_price"),
                "actual_entry": tr.get("entry_price"),
                "entry_slippage_pips": tr.get("slippage_pips"),
                "stop_px": tr.get("stop_loss"), "target_px": tr.get("take_profit"),
                "decision_id": tr.get("scalp_decision_id", ""),
                "est_cost_usd": 0.0,
            }
            if info["state"] == "CLOSE_REQUESTED":
                info["close_requested_ms"] = _iso_to_ms(pend.get("requested_at")) or now_ms()
                info["close_persisted"] = True   # intent already in the DB
            if tr.get("scalp_decision_id"):
                dec = await db.scalp_decisions.find_one(
                    {"decision_id": tr["scalp_decision_id"]},
                    {"cost_pips": 1, "lot": 1})
                if dec:
                    from pip_utils import pip_value_usd_per_lot_strict
                    pv = pip_value_usd_per_lot_strict(self.symbol)
                    if pv is not None:
                        info["est_cost_usd"] = round(
                            float(dec.get("cost_pips") or 0) * pv
                            * float(dec.get("lot") or 0), 2)
            self.live_trades[tid] = info
        self.risk_state.open_scalps = open_count
        try:
            await self.reconcile_commission(db)
        except Exception as e:  # noqa: BLE001 — advisory, never blocks restore
            logger.warning("commission reconciliation failed for %s %s: %s",
                           self.account_id, self.symbol, e)
        self._risk_restored = True

    async def reconcile_commission(self, db):
        """Round 14 item 8 — configured commission validated against the
        OBSERVED median per-lot commission from live broker fills. A
        mismatch is surfaced in status() and logged CRITICAL — a wrong
        commission assumption silently inflates scalp expectancy."""
        per_lot = []
        cur = db.scalp_decisions.find(
            {"account_id": self.account_id, "symbol": self.symbol,
             "dataset": "filled_live",
             "execution_outcome.commission_usd": {"$nin": [0, None]}},
            {"lot": 1, "execution_outcome.commission_usd": 1}
        ).sort("ts_ms", -1).limit(200)
        async for d in cur:
            lot = float(d.get("lot") or 0)
            com = abs(float((d.get("execution_outcome") or {})
                            .get("commission_usd") or 0))
            if lot > 0 and com > 0:
                per_lot.append(com / lot)
        check = {"configured_usd_per_lot_side": self.commission_usd_per_lot_side,
                 "observed_deals": len(per_lot),
                 "observed_median_usd_per_lot": None, "mismatch": False}
        if per_lot:
            per_lot.sort()
            med = per_lot[len(per_lot) // 2]
            check["observed_median_usd_per_lot"] = round(med, 3)
            cfgv = self.commission_usd_per_lot_side
            check["mismatch"] = (abs(med - cfgv) / cfgv > 0.5 if cfgv > 0
                                 else med > 0.5)
            if check["mismatch"]:
                logger.critical(
                    "scalp commission mismatch %s %s: configured %.2f/lot-side "
                    "vs observed median %.2f/lot — expectancy math suspect",
                    self.account_id, self.symbol,
                    self.commission_usd_per_lot_side, med)
        self.commission_check = check

    # ---------------- tick recording (Step 2, off hot path) ----------------

    def _maybe_flush_ticks(self, db):
        nm = now_ms()
        if (len(self._tick_buffer) >= TICK_FLUSH_N
                or (self._tick_buffer and nm - self._last_flush_ms >= TICK_FLUSH_MS)):
            batch, self._tick_buffer = self._tick_buffer, []
            self._last_flush_ms = nm
            _bg(lambda b=batch: db.scalp_ticks.insert_one({
                "user_id": self.user_id, "account_id": self.account_id,
                "symbol": self.symbol, "n": len(b),
                "first_ms": b[0]["tm"], "last_ms": b[-1]["tm"],
                "stored_at": datetime.now(timezone.utc).isoformat(),
                "ticks": b}), "tick_batch")

    def fill_stats(self) -> dict:
        """Empirical fill probability with an optimistic Beta(8,1) prior so a
        cold start never blocks; live rejections pull it down fast."""
        p = (self.exec_fills + 8.0) / (self.exec_attempts + 9.0)
        return {"attempts": self.exec_attempts, "fills": self.exec_fills,
                "p_fill": round(p, 3)}

    def status(self) -> dict:
        feats = snapshot(self.state)
        return {
            "account_id": self.account_id, "symbol": self.symbol,
            "enabled": self.enabled, "mode": self.mode,
            "model_key": self.model_key(),
            "risk_restored": self._risk_restored,
            "health": self.health,
            "block_reasons": sorted(account_block_reasons(self.account_id)),
            "broker_state_stale": broker_state_stale_reason(self.account),
            "capacity_integrity": capacity_integrity_reason(
                _broker_cap_key(self.broker)),
            # round 14 item 3 — per-aspect broker-state freshness: a single
            # heartbeat timestamp must not imply everything is current.
            "broker_state": {
                "heartbeat_at": (self.account or {}).get("last_heartbeat"),
                "heartbeat_age_sec": _iso_age_sec(
                    (self.account or {}).get("last_heartbeat")),
                "equity_age_sec": _iso_age_sec(
                    (self.account or {}).get("last_heartbeat")),
                "symbol_specs_updated_at":
                    (self.account or {}).get("symbol_specs_updated_at"),
                "symbol_specs_age_sec": _iso_age_sec(
                    (self.account or {}).get("symbol_specs_updated_at")),
                "spreads_age_sec": _iso_age_sec(
                    (self.account or {}).get("spreads_updated_at")),
                "connection_status": (self.account or {}).get("status"),
                "last_order_ack_ms": self.last_order_ack_ms,
            },
            "commission_check": self.commission_check,
            "permissions": permissions.get_cached(self.user_id, self.symbol),
            "counters": self.counters,
            "open_sims": len(self.open_sims),
            "open_live": len(self.live_trades),
            "fill_stats": self.fill_stats(),
            "awaiting_financials": len(self._closed_awaiting_financials),
            "live_states": {tid: i.get("state") for tid, i in self.live_trades.items()},
            "equity": self.equity,
            "spread_pips": self.state.spread_pips(),
            "quote_age_ms": self.state.quote_age_ms(),
            "clock_drift_ms": self.state.clock_drift_ms,
            "features": ({k: round(v, 3) for k, v in feats.items()
                          if not k.startswith("_")} if feats else None),
            "risk": {
                "consecutive_losses": self.risk_state.consecutive_losses,
                "daily_loss_usd": round(self.risk_state.daily_loss_usd, 2),
                "daily_cost_usd": round(self.risk_state.daily_cost_usd, 2),
                "open_scalps": self.risk_state.open_scalps,
            },
            "account_risk": {
                "consecutive_losses": self.account_risk.consecutive_losses,
                "daily_loss_usd": round(self.account_risk.daily_loss_usd, 2),
                "daily_cost_usd": round(self.account_risk.daily_cost_usd, 2),
                "open_scalps": self.account_risk.open_scalps,
            },
            "last_decision": ({k: v for k, v in self.last_decision.items()
                               if k != "features"} if self.last_decision else None),
        }


def _oid(s):
    from bson import ObjectId
    try:
        return ObjectId(s)
    except Exception:
        return s


def _iso_to_ms(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
                   .timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def _iso_age_sec(iso: str | None) -> int | None:
    ms = _iso_to_ms(iso)
    if ms is None:
        return None
    return max(0, int((now_ms() - ms) / 1000))


async def _resv_transition(db, reservation_id: str, state: str, **extra):
    """Round 18 review item 1 — AWAITED safety-critical reservation
    transition. A write failure is logged, never masks the caller's flow;
    the reservation sweep resolves any resulting lag conservatively."""
    try:
        await risk_reservations.transition(db, reservation_id, state, **extra)
    except Exception:  # noqa: BLE001
        logger.exception("reservation transition %s failed rid=%s",
                         state, reservation_id)


def _bg(factory, desc: str = ""):
    """Schedule a persistence op. `factory` is a CALLABLE returning a fresh
    coroutine — never a coroutine object — so retries can re-create it and
    nothing leaks when no loop is running (round 4 item 1)."""
    global _audit_pending
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    _audit_pending += 1
    loop.create_task(_run_operation(factory, desc))
    return True


async def _run_operation(factory, desc: str = ""):
    """One REAL retry (fresh coroutine), then durable file dead-letter."""
    global _audit_pending, _audit_failures
    try:
        for attempt in range(2):
            try:
                await factory()
                return
            except Exception as e:  # noqa: BLE001
                if attempt == 0:
                    await asyncio.sleep(0.5)
                else:
                    _audit_failures += 1
                    _dead_letter(desc, str(e))
    finally:
        _audit_pending = max(0, _audit_pending - 1)


DEAD_LETTER_PATH = os.environ.get(
    "SCALP_DEAD_LETTER_PATH",
    str(Path(__file__).resolve().parent.parent / "scalp_dead_letter.jsonl"))


def _dead_letter(desc: str, error: str):
    """Round 6 item 10 / round 7 item 10 — synchronous durable append FIRST
    (local JSONL), then best-effort async replication to the Mongo outbox.
    The file write completes before this function returns, so a failing or
    shutting-down process still leaves a durable record."""
    rec = {"ts": datetime.now(timezone.utc).isoformat(),
           "op": desc, "error": error, "pid": _owner_pid}
    try:
        import json
        p = Path(DEAD_LETTER_PATH)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(json.dumps(rec) + "\n")
            f.flush()
    except OSError:
        logger.error("scalp dead-letter write failed: %s / %s", desc, error)
    try:
        from database import get_db
        loop = asyncio.get_running_loop()
        loop.create_task(get_db().scalp_dead_letter.insert_one(dict(rec)))
    except Exception:  # noqa: BLE001 — replication is best-effort
        pass


def audit_backlog() -> dict:
    # counters are PROCESS-LOCAL (round 5 item 3): pid identifies the worker
    # that owns the runners; multi-worker deployments need sticky routing.
    return {"pending": _audit_pending, "failures": _audit_failures,
            "halted": _audit_pending >= AUDIT_BACKLOG_HALT,
            "pid": _owner_pid,
            "service_block": _service_block_reason,
            "account_blocks": {k: sorted(v)
                               for k, v in _account_blocks.items()},
            "invariant_scan": dict(_invariant_scan),
            "dead_letter_path": DEAD_LETTER_PATH}


def get_runner(account_id: str, user_id: str, symbol: str) -> ScalpRunner | None:
    if approved(symbol) is None:
        return None
    key = f"{account_id}:{symbol.upper()}"
    r = _runners.get(key)
    if r is None:
        if len(_runners) >= MAX_RUNNERS_PER_WORKER:
            # round 14 item 10 — fail closed instead of unbounded growth
            logger.critical(
                "scalp runner capacity %d reached — refusing new runner %s",
                MAX_RUNNERS_PER_WORKER, key)
            return None
        r = ScalpRunner(account_id, user_id, symbol.upper())
        _runners[key] = r
    return r


def runners_for_account(account_id: str) -> list:
    return [r for k, r in _runners.items() if k.startswith(f"{account_id}:")]


async def apply_broker_deal(db, account_id: str, trade: dict, *, deal_id,
                            lots: float, profit: float, commission: float,
                            swap: float, price, partial: bool,
                            remaining_lots: float | None = None,
                            occurred_at_iso: str | None = None,
                            recovery: bool = False) -> dict:
    """Round 7 critical item — the ONE way a broker deal reaches a scalp
    runner. Returns {"applied": bool, "reason": str|None}; callers may mark
    the broker deal reconciliation-complete ONLY when applied is True.
    Constructs and restores the runner if it does not exist yet."""
    from pip_utils import base_symbol
    symbol = base_symbol(trade.get("symbol") or "")
    # Round 12 item 2 — EXPLICIT ownership intent: a live broker callback
    # confirms (or first-touch adopts) current ownership; only the recovery
    # sweep may take over another worker's expired lease (logged takeover).
    if recovery:
        owned = await acquire_expired_ownership_for_recovery(db, account_id)
    else:
        owned = await confirm_or_adopt_account_lease(db, account_id)
    if not owned:
        return {"applied": False, "reason": "account_owned_by_other_worker"}
    r = get_runner(account_id, str(trade.get("user_id") or ""), symbol)
    if r is None:
        return {"applied": False, "reason": "runner_unavailable"}
    if not r._risk_restored:
        try:
            await r.restore_risk(db)
        except Exception as e:  # noqa: BLE001
            return {"applied": False, "reason": f"restore_failed: {e}"}
    # Round 10 item 2 — durable ledger-first state machine. The financial
    # event is built from immutable broker-deal facts and persisted as
    # PENDING before any risk mutation. On crash-recovery this upsert
    # re-creates a missing ledger event even when the runner's dedup cache
    # (applied_deal_ids restored from risk state) skips the risk mutation.
    from scalp.deals import build_financial_event
    ev = build_financial_event(
        account_id=account_id, symbol=symbol, trade_id=str(trade["_id"]),
        deal_id=deal_id,
        event_type="partial_close" if partial else "full_close",
        profit=profit, commission=commission, swap=swap,
        remaining_lots=(remaining_lots if partial else None),
        lease_epoch=_lease_epoch.get(account_id, 0),
        at_iso=occurred_at_iso)
    ev_key = {"account_id": ev["account_id"], "deal_id": ev["deal_id"],
              "event_type": ev["event_type"]}
    try:
        await db.scalp_financial_events.update_one(
            ev_key,
            {"$setOnInsert": {**ev, "status": "pending",
                              "risk_applied": False}}, upsert=True)
    except Exception as e:  # noqa: BLE001
        return {"applied": False, "reason": f"ledger_write_failed: {e}"}
    # Round 12 item 3 — explicit, idempotent state transition: mark the
    # (re-)application attempt so a crash window is visible in the ledger.
    try:
        await db.scalp_financial_events.update_one(
            {**ev_key, "status": {"$ne": "applied"}},
            {"$set": {"status": "risk_applying",
                      "last_attempt_at": datetime.now(timezone.utc).isoformat()},
             "$inc": {"apply_attempts": 1}})
    except Exception:  # noqa: BLE001 — telemetry only
        pass
    try:
        if partial:
            r.on_partial_close(
                trade_id=str(trade["_id"]), closed_lots=float(lots or 0),
                remaining_lots=(float(remaining_lots)
                                if remaining_lots is not None
                                else float(trade.get("lot_size") or 0)),
                pnl=float(profit or 0), commission=float(commission or 0),
                swap=float(swap or 0),
                exit_price=float(price) if price else None,
                deal_id=str(deal_id), db=db)
        else:
            r.on_trade_closed(
                trade_id=str(trade["_id"]), pnl=float(profit or 0),
                commission=float(commission or 0), swap=float(swap or 0),
                exit_price=float(price) if price else None,
                deal_id=str(deal_id), close_reason=trade.get("close_reason"),
                source="broker_deal", db=db)
        # item 6 — risk state + applied deal id are DURABLE before the
        # caller may flip the broker deal to complete
        await r.persist_risk_now(db)
        # round 18 review item 1 — reservation release is safety-relevant
        # state: awaited alongside the fenced risk persist.
        try:
            await risk_reservations.release_for_trade(
                db, str(trade["_id"]), "closed")
        except Exception:  # noqa: BLE001
            logger.exception("reservation release failed trade=%s",
                             str(trade["_id"]))
        # ledger event flips to APPLIED only after the fenced risk persist;
        # round 12 item 10 — record reconciliation latency (economic time →
        # server receipt → risk application) for lag monitoring.
        _applied_now = datetime.now(timezone.utc)
        _lat = {}
        try:
            _occ = datetime.fromisoformat(ev["occurred_at"])
            _rec = datetime.fromisoformat(ev["received_at"])
            _lat = {"report_delay_sec": round((_rec - _occ).total_seconds(), 3),
                    "reconcile_delay_sec": round(
                        (_applied_now - _rec).total_seconds(), 3)}
        except (ValueError, KeyError):
            pass
        await db.scalp_financial_events.update_one(
            ev_key,
            {"$set": {"status": "applied", "risk_applied": True,
                      "applied_at": _applied_now.isoformat(), **_lat}})
    except Exception as e:  # noqa: BLE001
        return {"applied": False, "reason": f"apply_failed: {e}"}
    return {"applied": True, "reason": None}


async def verify_durable_invariants(db) -> dict:
    """Round 9 item 3 — DATABASE-side invariants that cannot fail open when
    no runner is loaded: any account with open scalp trades in the DB but no
    restored runner stays BLOCKED until restoration completes; open scalps
    without a protective stop block their account too; a restored runner's
    live position set must equal the DB's open scalp set (broker == DB is
    enforced separately on every heartbeat by trade_reconciler)."""
    blocked = []
    _invariant_scan["last_attempt_at"] = datetime.now(timezone.utc).isoformat()
    scan_t0 = time.monotonic()
    docs_examined = 0
    # Round 12 P0 — NO global truncation on safety integrity checks: iterate
    # the full open-scalp cursor (grouped per account) instead of .to_list(200).
    by_account: dict = {}
    async for tr in db.trades.find(
            {"scope": "scalp_fast", "status": "open"}).sort("_id", 1):
        docs_examined += 1
        by_account.setdefault(str(tr.get("account_id")), []).append(tr)
    for account_id, trs in by_account.items():
        runners = runners_for_account(account_id)
        restored = any(r._risk_restored for r in runners)
        no_stop = [t for t in trs if not float(t.get("stop_loss") or 0)]
        position_mismatch = False
        if restored:
            db_ids = {str(t["_id"]) for t in trs}
            runner_ids = {tid for r in runners
                          for tid, lt in r.live_trades.items()
                          if lt.get("state") == "OPEN"}
            position_mismatch = db_ids != runner_ids
        if not restored or no_stop or position_mismatch:
            add_account_block(account_id, BLOCK_DURABLE_INVARIANT)
            blocked.append({"account_id": account_id,
                            "open_in_db": len(trs),
                            "runner_restored": restored,
                            "position_mismatch": position_mismatch,
                            "unstopped": len(no_stop)})
        else:
            # clean durable state — this sweep clears ONLY its own reason;
            # the in-memory checks own (and clear) invariant_violation
            clear_account_block(account_id, BLOCK_DURABLE_INVARIANT)
            if BLOCK_INVARIANT in account_block_reasons(account_id):
                verify_account_invariants(account_id)
    # accounts with no open scalp trades left cannot hold a durable-invariant
    # block — clear the sweep-owned reason (other reasons stay untouched)
    for acct in [a for a, rs in list(_account_blocks.items())
                 if BLOCK_DURABLE_INVARIANT in rs and a not in by_account]:
        clear_account_block(acct, BLOCK_DURABLE_INVARIANT)
    if blocked:
        logger.error("durable invariant violations: %s", blocked)
    # Round 10 item 4 — financial-ledger invariants (DB-side, runner-free):
    #   BLOCKING: every scalp broker deal reconciled in the last 24h must
    #   have its ledger event (a reconciled deal with no event means the
    #   ledger silently lost a financial fact).
    #   WARNING: per-account sum of today's applied negative net-P&L events
    #   should equal the persisted _ACCOUNT daily_loss_usd (report-path
    #   closes without a deal_id can legitimately diverge, so mismatches
    #   are surfaced for review instead of hard-blocking).
    fin_blocked, fin_warnings = [], []
    day_ago = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    # Round 11 item 3 — NO global fixed limit for financial integrity: an
    # aggregation left-joins every completed scalp deal of the window against
    # the ledger and returns EVERY missing event.
    missing_pipeline = [
        {"$match": {"financial_reconciliation_status": "complete",
                    "financial_reconciled_at": {"$gte": day_ago},
                    "reconciliation_note": {"$ne": "not_scalp_scope"}}},
        {"$lookup": {
            "from": "scalp_financial_events",
            "let": {"acct": "$account_id", "did": {"$toString": "$deal_id"}},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$account_id", "$$acct"]},
                    {"$eq": ["$deal_id", "$$did"]}]}}},
                {"$limit": 1}],
            "as": "ev"}},
        {"$match": {"ev": {"$size": 0}}},
        {"$project": {"account_id": 1, "deal_id": 1}},
    ]
    ledger_flagged: set = set()
    async for deal in db.broker_deals.aggregate(missing_pipeline):
        acct = str(deal["account_id"])
        add_account_block(acct, BLOCK_LEDGER)
        ledger_flagged.add(acct)
        fin_blocked.append({"account_id": acct,
                            "deal_id": str(deal["deal_id"]),
                            "issue": "reconciled_deal_missing_ledger_event"})
    for acct in [a for a, rs in list(_account_blocks.items())
                 if BLOCK_LEDGER in rs and a not in ledger_flagged]:
        clear_account_block(acct, BLOCK_LEDGER)
    # Round 11 item 5 — canonical daily metrics reconciled separately:
    # gross loss / net P&L per account from applied ledger events vs the
    # persisted _ACCOUNT snapshot (report-path closes without deal ids can
    # legitimately diverge → WARNING, not a block).
    # Round 13 item 6 — EXPLICIT UTC day boundary (offset-aware midnight),
    # matching RiskState's UTC daily_key convention.
    day_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0).isoformat()
    sums = {}
    async for g in db.scalp_financial_events.aggregate([
            {"$match": {"risk_applied": True, "at": {"$gte": day_start}}},
            {"$group": {
                "_id": "$account_id",
                "net": {"$sum": "$net_pnl"},
                "gross_loss": {"$sum": {"$cond": [
                    {"$lt": ["$net_pnl", 0]},
                    {"$subtract": [0, "$net_pnl"]}, 0]}}}}]):
        sums[str(g["_id"])] = g
    async for rs in db.scalp_risk_state.find({"symbol": "_ACCOUNT"}):
        acct = str(rs["account_id"])
        if rs.get("daily_key") and rs["daily_key"] != day_start[:10]:
            continue                       # snapshot belongs to a prior day
        g = sums.get(acct, {"net": 0.0, "gross_loss": 0.0})
        for metric, persisted_key in (
                ("gross_loss", "daily_gross_loss_usd"),
                ("net", "daily_net_pnl_usd")):
            persisted_val = rs.get(persisted_key)
            if persisted_val is None and persisted_key == "daily_gross_loss_usd":
                persisted_val = rs.get("daily_loss_usd")
            if persisted_val is None:
                continue                   # legacy snapshot without metric
            ledger_val = float(g.get(metric) or 0)
            persisted = float(persisted_val or 0)
            tol = max(0.02, 0.001 * max(abs(ledger_val), abs(persisted)))
            if abs(ledger_val - persisted) > tol:
                fin_warnings.append({
                    "account_id": acct,
                    "issue": f"daily_{metric}_ledger_mismatch",
                    "ledger_value": round(ledger_val, 2),
                    "persisted_value": round(persisted, 2)})
    if fin_blocked:
        logger.error("financial ledger invariant violations: %s", fin_blocked)
    if fin_warnings:
        logger.warning("financial ledger warnings: %s", fin_warnings)
    # Round 12 item 10 — reconciliation-lag guardrail: a pending ledger
    # event older than the max age means account risk may be stale → halt
    # new entries on that account until it reconciles.
    now_dt = datetime.now(timezone.utc)
    oldest_pending_sec = None
    pending_flagged: set = set()
    async for g in db.scalp_financial_events.aggregate([
            {"$match": {"status": {"$ne": "applied"}}},
            {"$group": {"_id": "$account_id",
                        "oldest": {"$min": "$received_at"}}}]):
        try:
            age = (now_dt - datetime.fromisoformat(g["oldest"])).total_seconds()
        except (ValueError, TypeError):
            continue
        oldest_pending_sec = max(oldest_pending_sec or 0, age)
        if age > PENDING_EVENT_MAX_AGE_SEC:
            acct = str(g["_id"])
            add_account_block(acct, BLOCK_PENDING_LEDGER)
            pending_flagged.add(acct)
            fin_blocked.append({"account_id": acct,
                                "issue": "pending_ledger_event_overdue",
                                "age_sec": int(age)})
    for acct in [a for a, rs in list(_account_blocks.items())
                 if BLOCK_PENDING_LEDGER in rs and a not in pending_flagged]:
        clear_account_block(acct, BLOCK_PENDING_LEDGER)
    _invariant_scan.update({
        "last_success_at": now_dt.isoformat(), "last_error": None,
        "last_duration_ms": int((time.monotonic() - scan_t0) * 1000),
        "docs_examined": docs_examined,
        "blocked_accounts": len(blocked) + len(fin_blocked),
        "ledger_mismatches": len(fin_warnings),
        "oldest_pending_event_sec": (int(oldest_pending_sec)
                                     if oldest_pending_sec is not None
                                     else None)})
    return {"blocked": blocked, "financial_blocked": fin_blocked,
            "financial_warnings": fin_warnings}


async def recover_pending_deals(db, older_than_sec: int = 60,
                                limit: int = 100) -> dict:
    """Round 6/7 — crash-recovery sweep. A deal flips to 'complete' ONLY
    after a runner confirmed the financial application; deals that are not
    scalp targets complete with an explicit note; failures stay pending
    with reconciliation_error + attempt count."""
    cutoff = (datetime.now(timezone.utc)
              - timedelta(seconds=older_than_sec)).isoformat()
    cur = db.broker_deals.find(
        {"financial_reconciliation_status": "pending",
         "received_at": {"$lt": cutoff}}).limit(limit)
    recovered = closed_out = kept_pending = 0
    async for deal in cur:
        key = {"deal_id": deal["deal_id"], "account_id": deal["account_id"]}
        now_iso = datetime.now(timezone.utc).isoformat()
        # Round 13 item 7 — TRANSACTIONAL sweep: atomically CLAIM the deal
        # before touching it, so two concurrent sweeps can never double-apply
        # the same pending deal; the claim expires after RECONCILE_CLAIM_TTL.
        claim = await db.broker_deals.update_one(
            {**key, "financial_reconciliation_status": "pending",
             "$or": [{"reconcile_claim_until": {"$exists": False}},
                     {"reconcile_claim_until": {"$lt": now_iso}}]},
            {"$set": {"reconcile_claim_until": (
                datetime.now(timezone.utc)
                + timedelta(seconds=RECONCILE_CLAIM_TTL_SEC)).isoformat(),
                "reconcile_claimed_by": _worker_id}})
        if claim.matched_count == 0:
            continue                # claimed by another sweep, or completed
        try:
            trade = await db.trades.find_one(
                {"account_id": deal["account_id"],
                 "mt5_ticket": deal.get("mt5_ticket")})
            if not trade or trade.get("scope") != "scalp_fast":
                if not trade:
                    # round 8 item 7 — a missing trade can be a RACE (deal
                    # arrived before trade adoption): grace-retry, then
                    # escalate to manual review; never silently complete.
                    attempts = int(deal.get("reconciliation_attempts") or 0)
                    if attempts < 5:
                        await db.broker_deals.update_one(key, {
                            "$set": {"reconciliation_error": "no_matching_trade_yet"},
                            "$inc": {"reconciliation_attempts": 1}})
                        kept_pending += 1
                    else:
                        await db.broker_deals.update_one(key, {"$set": {
                            "financial_reconciliation_status":
                                "manual_reconciliation_required",
                            "reconciliation_note": "no_matching_trade",
                            "escalated_at": now_iso}})
                        kept_pending += 1
                    continue
                # trade exists but is not a scalp target — definitive
                await db.broker_deals.update_one(
                    {**key,
                     "financial_reconciliation_status": {"$ne": "complete"}},
                    {"$set": {
                        "financial_reconciliation_status": "complete",
                        "financial_reconciled_at": now_iso,
                        "financial_reconciled_by": "recovery_job",
                        "reconciliation_note": "not_scalp_scope"}})
                closed_out += 1
                continue
            res = await apply_broker_deal(
                db, deal["account_id"], trade,
                deal_id=deal["deal_id"], lots=deal.get("lots") or 0,
                profit=deal.get("profit") or 0,
                commission=deal.get("commission") or 0,
                swap=deal.get("swap") or 0, price=deal.get("price"),
                partial=(trade.get("status") == "open"),
                occurred_at_iso=(deal.get("occurred_at")
                                 or deal.get("received_at")),
                recovery=True)
            if res["applied"]:
                await db.broker_deals.update_one(
                    {**key,
                     "financial_reconciliation_status": {"$ne": "complete"}},
                    {"$set": {
                        "financial_reconciliation_status": "complete",
                        "financial_reconciled_at": now_iso,
                        "financial_reconciled_by": "recovery_job"}})
                recovered += 1
                closed_out += 1
            else:
                await db.broker_deals.update_one(key, {
                    "$set": {"reconciliation_error": res["reason"]},
                    "$inc": {"reconciliation_attempts": 1}})
                kept_pending += 1
        except Exception as e:  # noqa: BLE001 — keep sweeping other deals
            logger.warning("scalp deal recovery failed deal=%s: %s",
                           deal.get("deal_id"), e)
            kept_pending += 1
    return {"recovered": recovered, "marked_complete": closed_out,
            "kept_pending": kept_pending}


async def apply_config(db, account: dict, symbol: str, enabled: bool, mode: str,
                       commission_usd_per_lot_side: float = 0.0):
    r = get_runner(str(account["_id"]), account["user_id"], symbol)
    if r is None:
        return None
    r.enabled = enabled
    r.mode = mode
    r.account = account
    r.broker = str(account.get("broker") or "")
    r.account_type = str(account.get("account_type") or "")
    r.commission_usd_per_lot_side = float(commission_usd_per_lot_side or 0.0)
    await r.restore_risk(db)
    await scalp_model.load_persisted(db, r.model_key())
    await db.scalp_configs.update_one(
        {"account_id": str(account["_id"]), "symbol": symbol.upper()},
        {"$set": {"user_id": account["user_id"], "enabled": enabled, "mode": mode,
                  "commission_usd_per_lot_side": r.commission_usd_per_lot_side,
                  "removed": False,
                  "updated_at": datetime.now(timezone.utc).isoformat()}},
        upsert=True)
    return r
