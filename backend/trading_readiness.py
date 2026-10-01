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

        def _hb(r):
            age = r.get("heartbeat_age_seconds")
            if age is None:
                return ("no EA heartbeat ever received — attach the STOIC "
                        "EA to a chart on this account")
            span = f"{age / 3600:.1f}h" if age >= 3600 else f"{age:.0f}s"
            return (f"position truth {r['position_truth']} — last EA "
                    f"heartbeat {span} ago; check the MT5 terminal/VPS is "
                    "running with the EA attached and AutoTrading ON")

        accs = [{"account_id": r["account_id"], "label": r.get("label"),
                 "reason": _hb(r)} for r in bad_truth]
        add("POSITION_TRUTH_STALE", "BLOCKED",
            f"{len(bad_truth)} account(s) with {worst} position truth — "
            "opening orders are blocked", accs,
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
                 "reason": r.get("state_reason"),
                 "trust_eligible": bool(r.get("trust_eligible"))}
                for r in disconnected]
        add("EXECUTION_BLOCKED", "BLOCKED",
            f"{len(disconnected)} enabled bot(s) cannot execute "
            f"({disconnected[0]['effective_state']})"
            + (f" — {causes[0]}" if len(causes) == 1 else ""), accs,
            causes[0] if len(causes) == 1 else
            "Each affected account below lists its exact blocking "
            "condition — resolve it to restore execution")

    # user-scoped execution truth — the SAME policy as the canonical
    # authority: UNKNOWN blocks immediately, broker-accepted states past
    # their settlement SLA and position mismatches block too (round 6 P1)
    acct_ids = [r["account_id"] for r in rows]
    if acct_ids:
        from datetime import datetime as _dt, timezone as _tz
        from execution_truth import (position_mismatches,
                                     unresolved_executions)
        _now_dt = _dt.now(_tz.utc)
        unresolved = await unresolved_executions(db, _now_dt,
                                                 account_ids=acct_ids)
        mism = await position_mismatches(db, _now_dt, account_ids=acct_ids)
        if unresolved or mism:
            unk = sum(1 for u in unresolved if u["status"] == "unknown")
            aged = len(unresolved) - unk
            bits = []
            if unk:
                bits.append(f"{unk} execution(s) in UNKNOWN state")
            if aged:
                bits.append(f"{aged} broker-accepted execution(s) past "
                            "their settlement SLA")
            if mism:
                bits.append(f"{len(mism)} account(s) with broker/local "
                            "open-position mismatch")
            add("RECONCILIATION_PENDING", "BLOCKED",
                " · ".join(bits) + " — broker reconciliation pending",
                [{"account_id": m["account_id"], "label": m.get("label"),
                  "reason": f"broker reports {m['broker_open']} open, "
                            f"local projection {m['local_open']}"}
                 for m in mism]
                + [{"account_id": u.get("account_id"), "label": None,
                    "reason": f"intent {u.get('intent_id')} "
                              f"{u['status']} for {u.get('age_s')}s "
                              f"(SLA {u['sla_s']}s)"}
                   for u in unresolved[:10]],
                "Run broker reconciliation; every broker-accepted execution "
                "must reach a terminal state and broker positions must "
                "match the local projection before new risk is allowed")

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

    # round 11 P1-04 — merge the canonical decision FIRST, de-duplicate and
    # severity-sort ONCE, then persist and return the SAME snapshot.
    from canonical_decision import decide_user, dominant, STATES
    dec = await decide_user(db, user_id)
    for b in dec["blockers"]:
        if not any(r["code"] == b["code"] for r in reasons):
            reasons.append({"code": b["code"], "level": b["state"], "message": b["reason"],
                            "accounts": ([{"account_id": str(b.get("account_id") or ""), "label": b["account_label"]}]
                                         if b.get("account_label") else []),
                            "recovery": b.get("recovery") or "See Safety Blocks → active platform blockers",
                            "source": "canonical_decision"})
    level = dominant(level, dec["state"]) if level in STATES else dec["state"]
    reasons.sort(key=lambda r: STATES.index(r["level"]) if r["level"] in STATES else 0, reverse=True)

    # first-seen persistence per user+code (cleared on recovery) — stored AFTER merge
    doc = await db.trading_readiness.find_one({"_id": user_id}) or {}
    seen = doc.get("first_seen") or {}
    now = _now()
    new_seen = {}
    for r in reasons:
        r["first_seen"] = seen.get(r["code"]) or now
        new_seen[r["code"]] = r["first_seen"]
    await db.trading_readiness.update_one(
        {"_id": user_id},
        {"$set": {"first_seen": new_seen, "level": level, "decision_id": dec["decision_id"],
                  "dominant_code": dec["dominant_code"], "reason_codes": [r["code"] for r in reasons],
                  "last_checked": now}},
        upsert=True)
    return {"level": level,
            "decision_id": dec["decision_id"],
            "dominant_code": dec["dominant_code"],
            "new_exposure_allowed": level in ("READY", "DEGRADED"),
            "reasons": reasons,
            "accounts_enabled": len(rows),
            "accounts_total": len(sc["accounts"]),
            "checked_at": now,
            "source": "canonical_decision + readiness enrichment",
            "note": "Trading readiness is independent of infrastructure "
                    "uptime — services can be operational while trading "
                    "is not safe."}
