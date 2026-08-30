"""Trading Readiness (audit F-02) — ONE user-readable trading state,
strictly separate from infrastructure uptime.

Levels (dominant → calm): EMERGENCY > BLOCKED > CLOSE_ONLY > DEGRADED >
READY. Each active reason carries a stable code, message, affected
accounts, first-seen time and the exact recovery action. First-seen is
persisted per user+code in `trading_readiness` and cleared on recovery.
"""
from datetime import datetime, timezone

LEVELS = ("READY", "DEGRADED", "CLOSE_ONLY", "BLOCKED", "EMERGENCY")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def worse(a: str, b: str) -> str:
    return a if LEVELS.index(a) >= LEVELS.index(b) else b


async def readiness(db, user_id: str) -> dict:
    from state_contract import contract
    sc = await contract(db, user_id)
    rows = [r for r in sc["accounts"] if r.get("account_enabled")]
    reasons: list[dict] = []
    level = "READY"

    def add(code: str, lvl: str, message: str, accounts: list,
            recovery: str) -> None:
        nonlocal level
        level = worse(level, lvl)
        reasons.append({"code": code, "level": lvl, "message": message,
                        "accounts": accounts, "recovery": recovery})

    def _ids(rs):
        return [{"account_id": r["account_id"], "label": r.get("label")}
                for r in rs]

    panic = [r for r in rows if r["effective_state"] == "PANIC"]
    if panic:
        add("PANIC_TRIPPED", "EMERGENCY",
            f"Panic switch tripped on {len(panic)} account(s) — all "
            "trading halted", _ids(panic),
            "Investigate the incident, verify broker positions, then reset "
            "the panic switch")

    bad_truth = [r for r in rows if r["position_truth"] != "FRESH"]
    if bad_truth:
        worst = max((r["position_truth"] for r in bad_truth),
                    key=lambda t: {"STALE": 1, "UNKNOWN": 2,
                                   "CONFLICTED": 3}.get(t, 1))
        add("POSITION_TRUTH_STALE", "BLOCKED",
            f"{len(bad_truth)} account(s) with {worst} position truth — "
            "opening orders are blocked", _ids(bad_truth),
            "Reconnect the MT5 EA; only a fresh broker snapshot plus "
            "successful reconciliation clears this (a restart never does)")

    disconnected = [r for r in rows
                    if r.get("bot_enabled")
                    and r["effective_state"] in ("BLOCKED", "DISCONNECTED")
                    and r["position_truth"] == "FRESH"]
    if disconnected:
        causes = sorted({r["state_reason"] for r in disconnected
                         if r.get("state_reason")})
        accs = [{"account_id": r["account_id"], "label": r.get("label"),
                 "reason": r.get("state_reason")} for r in disconnected]
        add("EXECUTION_BLOCKED", "BLOCKED",
            f"{len(disconnected)} enabled bot(s) cannot execute "
            f"({disconnected[0]['effective_state']})"
            + (f" — {causes[0]}" if len(causes) == 1 else ""), accs,
            causes[0] if len(causes) == 1 else
            "Each affected account below lists its exact blocking "
            "condition — resolve it to restore execution")

    # user-scoped unknown executions → reconciliation pending
    acct_ids = [r["account_id"] for r in rows]
    if acct_ids:
        unknown = await db.execution_intents.count_documents(
            {"status": "unknown", "account_id": {"$in": acct_ids}})
        if unknown:
            add("RECONCILIATION_PENDING", "BLOCKED",
                f"{unknown} execution(s) in UNKNOWN state — broker "
                "reconciliation pending", [],
                "Run broker reconciliation; UNKNOWN executions must reach "
                "a terminal state before new risk is allowed")

    reduced = [r for r in rows
               if r.get("bot_enabled")
               and r.get("execution_authority") not in (None, "FULL")]
    if reduced and level not in ("BLOCKED", "EMERGENCY"):
        add("AUTHORITY_REDUCED", "CLOSE_ONLY",
            f"{len(reduced)} account(s) with reduced execution authority "
            "— close/reduce only", _ids(reduced),
            "See the authority strip reasons; authority restores "
            "automatically once the underlying condition clears")

    if not rows:
        add("NO_ENABLED_ACCOUNTS", "DEGRADED",
            "No trading-enabled accounts — nothing can trade", [],
            "Enable an account from the header account switcher or the "
            "Accounts page")
    elif not any(r.get("bot_enabled") for r in rows):
        add("NO_BOTS_ENABLED", "DEGRADED",
            "Accounts are enabled but no bot is ON", [],
            "Turn a bot ON from Bot Config once you are ready")

    # first-seen persistence per user+code (cleared on recovery)
    doc = await db.trading_readiness.find_one({"_id": user_id}) or {}
    seen = doc.get("first_seen") or {}
    now = _now()
    new_seen = {}
    for r in reasons:
        r["first_seen"] = seen.get(r["code"]) or now
        new_seen[r["code"]] = r["first_seen"]
    await db.trading_readiness.update_one(
        {"_id": user_id},
        {"$set": {"first_seen": new_seen, "level": level,
                  "last_checked": now}},
        upsert=True)

    reasons.sort(key=lambda r: LEVELS.index(r["level"]), reverse=True)
    return {"level": level,
            "reasons": reasons,
            "accounts_enabled": len(rows),
            "accounts_total": len(sc["accounts"]),
            "checked_at": now,
            "note": "Trading readiness is independent of infrastructure "
                    "uptime — services can be operational while trading "
                    "is not safe."}
