"""Execution / broker truth (audit round 5 P1, round 6 P1): every
broker-accepted order must reach a terminal state and broker positions must
reconcile before new exposure. Read-only. Consumed by release-readiness
(`checks.execution_truth`), by the CANONICAL authority
(`trading_authority.execution_domain`) and by user trading readiness — one
policy, three surfaces.

Timing policy (round 6 — split, state-specific, asymmetric on purpose):
  * `unknown` (broker uncertainty)      → blocks IMMEDIATELY (SLA 0 s)
  * `submitted` / `dispatched`          → 120 s settlement SLA
  * `broker_pending` / `acked(nowledged)` → 300 s settlement SLA
Age is measured from the LAST transition (updated_at, else the newest
`transitions[].at`, else created_at); BSON datetimes and ISO strings both
count. Position mismatch on an enabled FRESH account fails closed.
"""
import hashlib
import os
from datetime import datetime, timedelta, timezone

from execution_intents import TERMINAL

_DEFAULT_SLA_S = {"unknown": 0, "submitted": 120, "dispatched": 120,
                  "broker_pending": 300, "acked": 300, "acknowledged": 300}


def _parse_sla(raw: str) -> dict:
    out = dict(_DEFAULT_SLA_S)
    for part in (raw or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            try:
                out[k.strip()] = max(0, int(v))
            except ValueError:
                pass
    return out


SLA_S = _parse_sla(os.environ.get("EXEC_TRUTH_SLA_S", ""))
UNKNOWN_MAX_AGE_S = SLA_S["unknown"]            # 0 → immediate (kept for callers/tests)
FRESH_HB_S = int(os.environ.get("EXEC_TRUTH_FRESH_HB_S", "600"))
NON_TERMINAL_AFTER_BROKER = tuple(SLA_S.keys())


def _to_dt(v):
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def _age(v, now):
    dt = _to_dt(v)
    return None if dt is None else (now - dt).total_seconds()


def last_transition_at(intent: dict):
    """updated_at → newest transitions[].at → created_at (BSON or ISO)."""
    for cand in (intent.get("updated_at"),):
        if _to_dt(cand) is not None:
            return cand
    ts = [t.get("at") for t in (intent.get("transitions") or []) if isinstance(t, dict) and _to_dt(t.get("at"))]
    if ts:
        return max(ts, key=lambda x: _to_dt(x))
    return intent.get("created_at")


def sla_for(status: str) -> int:
    return SLA_S.get(status, SLA_S["unknown"])


async def unresolved_executions(db, now: datetime, max_age_s: int | None = None,
                                account_ids: list | None = None) -> list:
    """Non-terminal intents older than their STATE-SPECIFIC SLA (or
    `max_age_s` for every state when given). Fetches in Python because the
    timestamp field/type varies per intent."""
    q: dict = {"status": {"$in": list(NON_TERMINAL_AFTER_BROKER)}}
    if account_ids is not None:
        q["account_id"] = {"$in": list(account_ids)}
    rows = await db.execution_intents.find(
        q, {"intent_id": 1, "status": 1, "account_id": 1, "created_at": 1, "updated_at": 1,
            "broker_ticket": 1, "request_id": 1, "response_id": 1, "transitions": 1, "symbol": 1}
    ).sort("created_at", 1).to_list(length=5000)
    out = []
    for r in rows:
        if r.get("status") in TERMINAL:
            continue
        age = _age(last_transition_at(r), now)
        if age is None:
            age = float("inf")            # untimestamped non-terminal intent = unknown age → fail closed
        sla = sla_for(r.get("status")) if max_age_s is None else max_age_s
        if age < sla:
            continue
        out.append({"intent_id": r.get("intent_id"), "status": r.get("status"), "account_id": r.get("account_id"),
                    "symbol": r.get("symbol"), "age_s": None if age == float("inf") else round(age),
                    "sla_s": sla, "broker_ticket": r.get("broker_ticket"), "request_id": r.get("request_id"),
                    "response_id": r.get("response_id"),
                    "transitions": [t.get("to") if isinstance(t, dict) else t for t in (r.get("transitions") or [])][-6:]})
    return out[:500]


async def position_mismatches(db, now: datetime, account_ids: list | None = None) -> list:
    q: dict = {"status": {"$ne": "deleted"}, "trading_enabled": True}
    if account_ids is not None:
        from bson import ObjectId
        ids = []
        for a in account_ids:
            try:
                ids.append(ObjectId(str(a)))
            except Exception:  # noqa: BLE001
                pass
        q["_id"] = {"$in": ids}
    accs = await db.accounts.find(q, {"label": 1, "last_heartbeat": 1, "open_positions": 1, "positions": 1}).to_list(length=2000)
    out = []
    for a in accs:
        age = _age(a.get("last_heartbeat"), now)
        if age is None or age > FRESH_HB_S:
            continue                     # stale → handled by position-truth STALE state, not counted as mismatch
        broker = a.get("open_positions")
        if broker is None:
            continue                     # no snapshot → UNKNOWN, never collapsed to zero here
        local = await db.trades.count_documents({"account_id": str(a["_id"]), "status": "open"})
        if int(broker) != local:
            phash = hashlib.sha256(repr(sorted((p.get("ticket"), p.get("symbol")) for p in (a.get("positions") or []))).encode()).hexdigest()[:16]
            out.append({"account_id": str(a["_id"]), "label": a.get("label"), "broker_open": int(broker),
                        "local_open": local, "heartbeat_age_s": round(age), "position_hash": phash})
    return out


def authority_for(unresolved: list, mismatches: list) -> dict:
    """The ONE mapping every surface uses: any mismatch or expired
    broker-accepted state → CLOSE_ONLY; UNKNOWN blocks immediately."""
    if not unresolved and not mismatches:
        return {"level": "FULL", "reason": "all executions terminal and positions reconciled"}
    parts = []
    unk = [u for u in unresolved if u["status"] == "unknown"]
    aged = [u for u in unresolved if u["status"] != "unknown"]
    if unk:
        parts.append(f"{len(unk)} execution(s) UNKNOWN after broker uncertainty — blocked immediately")
    if aged:
        parts.append(f"{len(aged)} broker-accepted execution(s) past their settlement SLA "
                     f"({', '.join(sorted({u['status'] for u in aged}))})")
    if mismatches:
        parts.append(f"{len(mismatches)} account(s) with broker/local open-position mismatch")
    return {"level": "CLOSE_ONLY", "reason": "; ".join(parts) + " — no new exposure until broker truth is terminal and positions reconcile"}


async def execution_truth_check(db, account_ids: list | None = None) -> dict:
    now = datetime.now(timezone.utc)
    unresolved = await unresolved_executions(db, now, account_ids=account_ids)
    mismatches = await position_mismatches(db, now, account_ids=account_ids)
    auth = authority_for(unresolved, mismatches)
    return {"ok": auth["level"] == "FULL", "unresolved_executions": len(unresolved),
            "position_mismatches": len(mismatches), "sla_s": dict(SLA_S),
            "max_unknown_age_s": UNKNOWN_MAX_AGE_S,
            "authority": auth["level"], "authority_reason": auth["reason"],
            "authority_if_failed": "CLOSE_ONLY — no new exposure until broker truth is terminal and positions reconcile",
            "sample": {"unresolved": unresolved[:5], "mismatches": mismatches[:5]},
            "checked_at": now.isoformat()}
