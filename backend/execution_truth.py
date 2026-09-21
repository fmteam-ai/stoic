"""Execution / broker truth (audit round 5 P1): every broker-accepted order
must reach a terminal state and broker positions must reconcile before new
exposure. Read-only. Used by release-readiness (`checks.execution_truth`)
and by `ops/execution_truth_report.py`.

Fails closed when:
  * an execution intent is non-terminal and older than UNKNOWN_MAX_AGE_S
    (default 900 s) — includes `unknown`, `submitted`, `dispatched`,
    `broker_pending`, `acked`, `acknowledged`;
  * an enabled account with a FRESH heartbeat reports a broker open-position
    count that differs from the local projection (position mismatch).
"""
import hashlib
import os
from datetime import datetime, timedelta, timezone

from execution_intents import TERMINAL

UNKNOWN_MAX_AGE_S = int(os.environ.get("UNKNOWN_MAX_AGE_S", "900"))
FRESH_HB_S = int(os.environ.get("EXEC_TRUTH_FRESH_HB_S", "600"))
NON_TERMINAL_AFTER_BROKER = ("submitted", "dispatched", "broker_pending", "acked", "acknowledged", "unknown")


def _age(iso, now):
    try:
        return (now - datetime.fromisoformat(str(iso).replace("Z", "+00:00"))).total_seconds()
    except Exception:  # noqa: BLE001
        return None


async def unresolved_executions(db, now: datetime, max_age_s: int = UNKNOWN_MAX_AGE_S) -> list:
    cutoff = (now - timedelta(seconds=max_age_s)).isoformat()
    rows = await db.execution_intents.find(
        {"status": {"$in": list(NON_TERMINAL_AFTER_BROKER)}, "created_at": {"$lt": cutoff}},
        {"intent_id": 1, "status": 1, "account_id": 1, "created_at": 1, "updated_at": 1,
         "broker_ticket": 1, "request_id": 1, "response_id": 1, "transitions": 1, "symbol": 1}
    ).sort("created_at", 1).to_list(length=500)
    out = []
    for r in rows:
        out.append({"intent_id": r.get("intent_id"), "status": r.get("status"), "account_id": r.get("account_id"),
                    "symbol": r.get("symbol"), "age_s": round(_age(r.get("created_at"), now) or 0),
                    "broker_ticket": r.get("broker_ticket"), "request_id": r.get("request_id"),
                    "response_id": r.get("response_id"),
                    "transitions": [t.get("to") if isinstance(t, dict) else t for t in (r.get("transitions") or [])][-6:]})
    return out


async def position_mismatches(db, now: datetime) -> list:
    accs = await db.accounts.find({"status": {"$ne": "deleted"}, "trading_enabled": True},
                                  {"label": 1, "last_heartbeat": 1, "open_positions": 1, "positions": 1}).to_list(length=2000)
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


async def execution_truth_check(db) -> dict:
    now = datetime.now(timezone.utc)
    unresolved = await unresolved_executions(db, now)
    mismatches = await position_mismatches(db, now)
    ok = not unresolved and not mismatches
    return {"ok": ok, "unresolved_executions": len(unresolved), "position_mismatches": len(mismatches),
            "max_unknown_age_s": UNKNOWN_MAX_AGE_S,
            "authority_if_failed": "CLOSE_ONLY — no new exposure until broker truth is terminal and positions reconcile",
            "sample": {"unresolved": unresolved[:5], "mismatches": mismatches[:5]},
            "checked_at": now.isoformat()}
