"""Canonical trading decision (audit round 9 P0-01) — ONE server-side object
that every surface (readiness, execution mode, Safety Blocks, promotion and
every frontend label) consumes. Derived from the trading-authority snapshot;
nothing else may derive "ready / executing / free / certified".

States, dominance order:  EMERGENCY > BLOCKED > CLOSE_ONLY > DEGRADED > READY
Level mapping:  FULL→READY · REDUCED→DEGRADED · CLOSE_ONLY→CLOSE_ONLY ·
                PAUSED/LOCKED→BLOCKED · EMERGENCY→EMERGENCY
"""
import os
import uuid
from datetime import datetime, timezone

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
        "risk_reducing_allowed": state != "EMERGENCY" or True,
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


async def decide_user(db, user_id: str) -> dict:
    """User-facing canonical decision: dominant across the platform view and
    every account of the user; per-account decisions attached."""
    platform = await decide_platform(db)
    per_account = []
    async for acc in db.accounts.find({"user_id": user_id}):
        d = await decide_account(db, acc)
        d["label"] = acc.get("label") or acc.get("account_login")
        d["environment"] = acc.get("mode") or "demo"
        per_account.append(d)
    state = dominant(platform["state"], *(d["state"] for d in per_account))
    codes = list(dict.fromkeys(platform["reason_codes"] + [c for d in per_account for c in d["reason_codes"]]))
    return {**platform, "decision_id": f"dec_{uuid.uuid4().hex[:12]}", "scope": "user",
            "state": state, "new_exposure_allowed": state in NEW_EXPOSURE_ALLOWED,
            "reason_codes": codes, "dominant_code": codes[0] if codes else "READY",
            "accounts": per_account,
            "stability_window_seconds": stability_window_seconds()}


def stability_window_seconds() -> int:
    try:
        return max(0, int(os.environ.get("AUTHORITY_STABILITY_WINDOW_SECONDS", "300")))
    except ValueError:
        return 300


def denial(decision: dict, path: str) -> dict:
    """Deterministic denial payload for every new-order path."""
    return {"blocked": "trading_authority", "path": path, "decision_id": decision["decision_id"],
            "state": decision["state"], "authority_level": decision.get("level"),
            "reason_codes": decision["reason_codes"],
            "reasons": [b["reason"] for b in decision["blockers"]]}
