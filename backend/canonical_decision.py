"""Canonical trading decision (audit round 9 P0-01) — ONE server-side object
that every surface (readiness, execution mode, Safety Blocks, promotion and
every frontend label) consumes. Derived from the trading-authority snapshot;
nothing else may derive "ready / executing / free / certified".

States, dominance order:  EMERGENCY > BLOCKED > CLOSE_ONLY > DEGRADED > READY
Level mapping:  FULL→READY · REDUCED→DEGRADED · CLOSE_ONLY→CLOSE_ONLY ·
                PAUSED/LOCKED→BLOCKED · EMERGENCY→EMERGENCY
"""
import hashlib
import os
import uuid
from datetime import datetime, timedelta, timezone

from pymongo.errors import DuplicateKeyError

STATES = ["READY", "DEGRADED", "CLOSE_ONLY", "BLOCKED", "EMERGENCY"]
LEVEL_TO_STATE = {"FULL": "READY", "REDUCED": "DEGRADED", "CLOSE_ONLY": "CLOSE_ONLY",
                  "PAUSED": "BLOCKED", "LOCKED": "BLOCKED", "EMERGENCY": "EMERGENCY"}
NEW_EXPOSURE_ALLOWED = {"READY", "DEGRADED"}

# stable, machine-readable reason codes per domain × level
_CODES = {
    "platform": "PLATFORM_OVERRIDE", "account": "ACCOUNT_RESTRICTED", "broker": "BROKER_UNCERTAIN",
    "risk": "SAFETY_BLOCKS", "pamm": "PAMM_STATE", "execution": "EXECUTION_UNKNOWN",
    "position_truth": "POSITION_TRUTH_STALE", "infrastructure": "TERMINAL_STALE",
    "certification": "CERTIFICATION_FAILED", "bot_health": "BOT_HEALTH_CRITICAL",
    "performance_truth": "PERFORMANCE_UNRECONCILED", "recovery": "RECOVERY_WINDOW",
    "inventory": "INVENTORY_DRIFT",
}


def severity(state: str) -> int:
    return STATES.index(state)


def dominant(*states: str) -> str:
    return max(states, key=severity)


def state_for_level(level: str) -> str:
    return LEVEL_TO_STATE.get(level, "CLOSE_ONLY")


def reason_code(domain: str, domain_state: dict) -> str:
    if domain_state.get("code"):
        return str(domain_state["code"])
    base = _CODES.get(domain, domain.upper())
    if domain == "account" and "identity mismatch" in (domain_state.get("reason") or ""):
        return "IDENTITY_MISMATCH"
    if domain == "account" and "trading_enabled" in (domain_state.get("reason") or ""):
        return "ACCOUNT_DISABLED"
    return base


def from_snapshot(snap: dict) -> dict:
    """Project a trading_authority.compute_authority snapshot into the decision."""
    blockers = []
    for name, d in (snap.get("domains") or {}).items():
        st = state_for_level(d.get("level", "CLOSE_ONLY"))
        if st != "READY":
            blockers.append({"domain": name, "code": reason_code(name, d), "state": st,
                             "level": d.get("level"), "reason": d.get("reason")})
    blockers.sort(key=lambda b: -severity(b["state"]))
    if snap.get("unavailable_domains"):
        blockers.append({"domain": "authority", "code": "DOMAIN_UNAVAILABLE", "state": "CLOSE_ONLY",
                         "level": "CLOSE_ONLY",
                         "reason": "domain(s) unavailable: " + ", ".join(snap["unavailable_domains"])})
    state = dominant("READY", *(b["state"] for b in blockers))
    if state == "DEGRADED" and not snap.get("hard_truth_fresh", True):
        state = "CLOSE_ONLY"
        blockers.append({"domain": "authority", "code": "HARD_TRUTH_NOT_FRESH", "state": "CLOSE_ONLY",
                         "level": "CLOSE_ONLY", "reason": "REDUCED requires fresh position/broker/execution truth"})
    return {
        "decision_id": f"dec_{uuid.uuid4().hex[:12]}",
        "snapshot_id": snap.get("snapshot_id"),
        "state": state,
        "level": snap.get("level"),
        "new_exposure_allowed": state in NEW_EXPOSURE_ALLOWED,
        # Policy: risk-REDUCING actions (close / cancel / hedge-off) stay allowed in every
        # state including EMERGENCY so operators can always shrink exposure.
        "risk_reducing_allowed": True,
        "close_allowed": True,
        "cancel_pending_allowed": True,
        "dominant_code": blockers[0]["code"] if blockers else "READY",
        "reason_codes": [b["code"] for b in blockers],
        "blockers": blockers,
        "domains": {k: {"level": v.get("level"), "state": state_for_level(v.get("level", "CLOSE_ONLY")),
                        "reason": v.get("reason")} for k, v in (snap.get("domains") or {}).items()},
        "scope": snap.get("scope"),
        "account_id": snap.get("account_id"),
        "computed_at": snap.get("computed_at") or datetime.now(timezone.utc).isoformat(),
        "dominance": STATES[::-1],
    }


async def decide_account(db, account: dict) -> dict:
    from trading_authority import compute_authority
    return from_snapshot(await compute_authority(db, account))


async def decide_platform(db) -> dict:
    from trading_authority import compute_authority
    return from_snapshot(await compute_authority(db, None))


_SNAPSHOT_TTL_S = 5.0
VERSION_ID = "authority_version"


async def authority_version(db) -> int:
    doc = await db.platform_state.find_one({"_id": VERSION_ID}, {"version": 1})
    return int((doc or {}).get("version") or 0)


async def bump_authority_version(db, reason: str, *, user_id: str | None = None, session=None) -> int:
    """Round 12 P2-02: invalidate EVERY cached canonical view (all workers/pods)
    the moment an input changes — PANIC, reconciliation, execution truth,
    inventory, identity, Bot Health, break-glass or a platform blocker."""
    doc = await db.platform_state.find_one_and_update(
        {"_id": VERSION_ID}, {"$inc": {"version": 1},
                              "$set": {"reason": reason[:120], "at": datetime.now(timezone.utc).isoformat(),
                                       "user_id": user_id}},
        upsert=True, return_document=True, session=session)
    await db.canonical_decisions.delete_many({} if user_id is None else {"_id": user_id}, session=session)
    return int(doc["version"])


async def inventory_fingerprint(db, user_id: str) -> str:
    """Cheap digest of the user's account/bot inventory inputs (ids, enabled,
    mode, bot active) — any add/remove/toggle invalidates the snapshot even when
    the writer did not call bump_authority_version()."""
    import hashlib
    import json
    rows = []
    async for a in db.accounts.find({"user_id": user_id}, {"_id": 1, "trading_enabled": 1, "mode": 1, "verified_identity": 1,
                                                          "status": 1, "reconciliation_seq": 1, "last_reconciled_at": 1,
                                                          "position_truth": 1, "execution_authority": 1,
                                                          "ea_version": 1, "ea_binary_sha256": 1, "last_heartbeat": 1}):
        hb = str(a.get("last_heartbeat") or "")
        rows.append(["a", str(a["_id"]), a.get("trading_enabled") is True, str(a.get("mode") or ""), bool(a.get("verified_identity")),
                     str(a.get("status") or ""), int(a.get("reconciliation_seq") or 0), str(a.get("last_reconciled_at") or ""),
                     str(a.get("position_truth") or ""), str(a.get("execution_authority") or ""),
                     # r20 P2-04: EA capability inputs + heartbeat freshness bucket (fresh ≤180 s / stale)
                     str(a.get("ea_version") or ""), str(a.get("ea_binary_sha256") or ""),
                     "fresh" if hb and hb >= (datetime.now(timezone.utc) - timedelta(seconds=180)).isoformat() else "stale"])
    async for b in db.bot_configs.find({"user_id": user_id}, {"account_id": 1, "active": 1}):
        rows.append(["b", str(b.get("account_id")), bool(b.get("active"))])
    async for p in db.platform_state.find({"_id": {"$in": ["trading_authority", "turnstile_break_glass", "inventory_expectation",
                                                            "inventory_hash_pending"]}}):
        rows.append(["p", str(p["_id"]), str(p.get("level") or ""), bool(p.get("active")), str(p.get("set_at") or p.get("until") or "")])
    rows.sort()
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


async def decide_user(db, user_id: str, *, fresh: bool = False) -> dict:
    """User-facing canonical decision (dominant across platform + accounts).
    The snapshot is PERSISTED (db.canonical_decisions) with the input version
    and inventory fingerprint it was computed from, so every worker/pod serves
    the same decision_id and any version bump or inventory change invalidates
    it before the next response; fresh=True forces recomputation."""
    ver = await authority_version(db)
    fp = await inventory_fingerprint(db, user_id)
    if not fresh:
        hit = await db.canonical_decisions.find_one({"_id": user_id})
        if hit and hit.get("input_version") == ver and hit.get("inventory_fingerprint") == fp:
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(hit["cached_at"])).total_seconds()
            except (KeyError, ValueError, TypeError):
                age = _SNAPSHOT_TTL_S
            if 0 <= age < _SNAPSHOT_TTL_S:
                return hit["decision"]
    out = await _decide_user_uncached(db, user_id)
    out["input_version"] = ver
    out["input_hash"] = hashlib.sha256(f"{ver}:{fp}".encode()).hexdigest()[:16]
    now = datetime.now(timezone.utc)
    stale_cutoff = (now - timedelta(seconds=_SNAPSHOT_TTL_S)).isoformat()
    snap = {"input_version": ver, "inventory_fingerprint": fp, "decision": out, "cached_at": now.isoformat()}
    # single-flight (round 13 P2-01): concurrent cold-cache computations race, but only
    # ONE decision is persisted per (version, fingerprint) window — losers return the winner.
    try:
        res = await db.canonical_decisions.update_one(
            {"_id": user_id, "$or": [{"input_version": {"$ne": ver}}, {"inventory_fingerprint": {"$ne": fp}},
                                     {"cached_at": {"$lt": stale_cutoff}}]},
            {"$set": snap}, upsert=True)
        if res.matched_count == 0 and res.upserted_id is None:
            raise DuplicateKeyError("snapshot exists")
    except DuplicateKeyError:
        hit = await db.canonical_decisions.find_one({"_id": user_id})
        if hit and hit.get("input_version") == ver and hit.get("inventory_fingerprint") == fp:
            return hit["decision"]
    return out


async def _decide_user_uncached(db, user_id: str) -> dict:
    platform = await decide_platform(db)
    per_account = []
    async for acc in db.accounts.find({"user_id": user_id}):
        d = await decide_account(db, acc)
        d["label"] = acc.get("label") or acc.get("account_login")
        d["environment"] = acc.get("mode") or "demo"
        per_account.append(d)
    # ONE flattened blocker list, sorted by severity (round 10 P1-03): state,
    # dominant_code, reason_codes and top-level blockers all derive from it.
    flat = [{**b, "account_id": None, "scope": "platform"} for b in platform["blockers"]]
    for d in per_account:
        flat += [{**b, "account_id": d["account_id"], "account_label": d.get("label"), "scope": "account"}
                 for b in d["blockers"]]
    flat.sort(key=lambda b: -severity(b["state"]))
    state = dominant(platform["state"], *(d["state"] for d in per_account))
    codes = list(dict.fromkeys(b["code"] for b in flat))
    return {**platform, "decision_id": f"dec_{uuid.uuid4().hex[:12]}", "scope": "user",
            "state": state, "new_exposure_allowed": state in NEW_EXPOSURE_ALLOWED,
            "blockers": flat, "reason_codes": codes,
            "dominant_code": flat[0]["code"] if flat else "READY",
            "dominant_account_id": flat[0]["account_id"] if flat else None,
            "accounts": per_account,
            "snapshot_ttl_seconds": _SNAPSHOT_TTL_S,
            "stability_window_seconds": stability_window_seconds()}


def stability_window_seconds() -> int:
    try:
        return max(0, int(os.environ.get("AUTHORITY_STABILITY_WINDOW_SECONDS", "300")))
    except ValueError:
        return 300


def denial(decision: dict, path: str) -> dict:
    """Deterministic denial payload for every new-order path."""
    return {"blocked": "trading_authority", "path": path, "decision_id": decision["decision_id"],
            "input_version": decision.get("input_version"), "input_hash": decision.get("input_hash"),
            "state": decision["state"], "authority_level": decision.get("level"),
            "reason_codes": decision["reason_codes"],
            "reasons": [b["reason"] for b in decision["blockers"]]}
