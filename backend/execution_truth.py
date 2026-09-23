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


EXEMPLAR_LIMIT = 500


async def unresolved_backlog(db, now: datetime, max_age_s: int | None = None,
                             account_ids: list | None = None) -> dict:
    """EXACT backlog accounting (round 7 P2): streams every non-terminal
    intent (no source cap), counts aged ones by status/account exactly and
    keeps the OLDEST `EXEMPLAR_LIMIT` as actionable exemplars. `truncated`
    tells the reader the exemplar list is partial; counts never are."""
    q: dict = {"status": {"$in": list(NON_TERMINAL_AFTER_BROKER)}}
    if account_ids is not None:
        q["account_id"] = {"$in": list(account_ids)}
    proj = {"intent_id": 1, "status": 1, "account_id": 1, "created_at": 1, "updated_at": 1, "broker_ticket": 1,
            "request_id": 1, "response_id": 1, "transitions": 1, "symbol": 1}
    total, by_status, by_account, scanned = 0, {}, {}, 0
    exemplars: list = []
    newest_unknown_age = None
    async for r in db.execution_intents.find(q, proj).sort("created_at", 1):
        scanned += 1
        if r.get("status") in TERMINAL:
            continue
        age = _age(last_transition_at(r), now)
        if age is None:
            age = float("inf")
        sla = sla_for(r.get("status")) if max_age_s is None else max_age_s
        if age < sla:
            continue
        total += 1
        by_status[r.get("status")] = by_status.get(r.get("status"), 0) + 1
        by_account[str(r.get("account_id"))] = by_account.get(str(r.get("account_id")), 0) + 1
        if r.get("status") == "unknown" and age != float("inf"):
            newest_unknown_age = age if newest_unknown_age is None else min(newest_unknown_age, age)
        if len(exemplars) < EXEMPLAR_LIMIT:
            exemplars.append({"intent_id": r.get("intent_id"), "status": r.get("status"), "account_id": r.get("account_id"),
                              "symbol": r.get("symbol"), "age_s": None if age == float("inf") else round(age),
                              "sla_s": sla, "broker_ticket": r.get("broker_ticket"), "request_id": r.get("request_id"),
                              "response_id": r.get("response_id"),
                              "transitions": [t.get("to") if isinstance(t, dict) else t for t in (r.get("transitions") or [])][-6:]})
    return {"total": total, "by_status": by_status, "by_account": by_account, "scanned": scanned,
            "exemplars": exemplars, "truncated": total > len(exemplars), "exemplar_limit": EXEMPLAR_LIMIT,
            "newest_unknown_age_s": None if newest_unknown_age is None else round(newest_unknown_age)}


async def unresolved_executions(db, now: datetime, max_age_s: int | None = None,
                                account_ids: list | None = None) -> list:
    """Oldest actionable exemplars (≤ EXEMPLAR_LIMIT). Non-empty iff the exact
    backlog is non-empty — authority decisions may rely on that."""
    return (await unresolved_backlog(db, now, max_age_s=max_age_s, account_ids=account_ids))["exemplars"]


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


def authority_for(unresolved: list, mismatches: list, by_status: dict | None = None) -> dict:
    """The ONE mapping every surface uses: any mismatch or expired
    broker-accepted state → CLOSE_ONLY; UNKNOWN blocks immediately.
    `by_status` (exact counts) makes the reason exact even when the exemplar
    list is truncated."""
    if not unresolved and not mismatches:
        return {"level": "FULL", "reason": "all executions terminal and positions reconciled"}
    parts = []
    if by_status is None:
        by_status = {}
        for u in unresolved:
            by_status[u["status"]] = by_status.get(u["status"], 0) + 1
    n_unk = by_status.get("unknown", 0)
    aged_states = {k: v for k, v in by_status.items() if k != "unknown" and v}
    if n_unk:
        parts.append(f"{n_unk} execution(s) UNKNOWN after broker uncertainty — blocked immediately")
    if aged_states:
        parts.append(f"{sum(aged_states.values())} broker-accepted execution(s) past their settlement SLA "
                     f"({', '.join(sorted(aged_states))})")
    if mismatches:
        parts.append(f"{len(mismatches)} account(s) with broker/local open-position mismatch")
    return {"level": "CLOSE_ONLY", "reason": "; ".join(parts) + " — no new exposure until broker truth is terminal and positions reconcile"}


async def execution_truth_check(db, account_ids: list | None = None) -> dict:
    now = datetime.now(timezone.utc)
    backlog = await unresolved_backlog(db, now, account_ids=account_ids)
    unresolved = backlog["exemplars"]
    mismatches = await position_mismatches(db, now, account_ids=account_ids)
    auth = authority_for(unresolved, mismatches, backlog["by_status"])
    return {"ok": auth["level"] == "FULL", "unresolved_executions": backlog["total"],
            "unresolved_by_status": backlog["by_status"], "unresolved_truncated": backlog["truncated"],
            "unresolved_exemplars_shown": len(unresolved), "newest_unknown_age_s": backlog["newest_unknown_age_s"],
            "position_mismatches": len(mismatches), "sla_s": dict(SLA_S),
            "max_unknown_age_s": UNKNOWN_MAX_AGE_S,
            "authority": auth["level"], "authority_reason": auth["reason"],
            "authority_if_failed": "CLOSE_ONLY — no new exposure until broker truth is terminal and positions reconcile",
            "sample": {"unresolved": unresolved[:5], "mismatches": mismatches[:5]},
            "checked_at": now.isoformat()}
