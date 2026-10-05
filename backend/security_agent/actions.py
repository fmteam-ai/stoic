"""SA4 — containment actions (enforce mode). Seven safety rules are enforced in code, not config:
protected list, hourly cap, no trade actions, every block expires, one-click undo, append-chained audit,
observe mode = dry run. Actions never touch trades, SL/TP, bot settings or the protection/trade-manager loops."""
import logging
from datetime import datetime, timedelta, timezone

from bson import ObjectId

from security_agent import rules
from security_agent.findings import open_or_update
from security_agent.checks.common import f
from security_agent.redact import mask

log = logging.getLogger("security_agent.actions")
BLOCK_ACTIONS = {"block_ip": "ip", "lock_login": "account_login", "lock_otp": "account_otp"}
USER_NOTICE = {
    "lock_login": ("Login temporarily locked", "Password login on your account was locked for {min} min after repeated failed attempts. Sessions verified with 2FA stay active."),
    "lock_otp": ("2FA verification paused", "2FA/OTP verification was paused for {min} min after repeated wrong codes. Your password alone cannot open the account."),
    "suspend_bridge_token": ("EA token suspended", "The bridge token of account {label} was suspended because it was used from two places at once. Re-pair the EA (Accounts → Rotate token) to continue."),
    "freeze_new_entries": ("New trades paused", "New trades on account {label} are paused pending a security check. Open positions stay fully managed (stops, exits, closes)."),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _oid(v):
    return ObjectId(v) if ObjectId.is_valid(str(v)) else v


async def _audit(db, action: str, target: str, meta: dict, actor: str = "security_agent") -> None:
    from audit_chain import append_chained
    await append_chained(db, {"actor_email": actor, "action": action, "target_kind": "security_agent", "target_id": str(target),
                              "target_label": "", "reason": "", "meta": meta, "at": _now().isoformat()})


async def _notify_owner(db, user_id: str | None, action: str, label: str, minutes: int) -> None:
    if not user_id or action not in USER_NOTICE:
        return
    title, msg = USER_NOTICE[action]
    try:
        await db.notifications.insert_one({"user_id": str(user_id), "kind": f"security_{action}", "title": title, "severity": "warning", "read": False,
                                           "message": msg.format(min=minutes, label=label), "created_at": _now().isoformat()})
        from notifier import send_telegram
        await send_telegram(str(user_id), "protection", title, [msg.format(min=minutes, label=label)])
    except Exception as e:  # noqa: BLE001
        log.warning("owner notice failed: %s", type(e).__name__)


async def _owner_of_account(db, account_id) -> tuple[str | None, str]:
    acc = await db.accounts.find_one({"_id": _oid(account_id)}, {"user_id": 1, "label": 1})
    return ((acc or {}).get("user_id"), (acc or {}).get("label") or str(account_id))


async def _owner_of_email(db, email: str) -> str | None:
    u = await db.users.find_one({"email": str(email).lower()}, {"_id": 1})
    return str(u["_id"]) if u else None


async def execute(db, cfg: dict, prop: dict, finding: dict) -> dict:
    """Apply one proposal. Returns the security_actions row (status done | refused_* | would_have_done | pending_mode)."""
    assert prop["action"] not in rules.FORBIDDEN_ACTIONS
    now = _now()
    since = (now - timedelta(hours=1)).isoformat()
    row = {"kind": "containment", "dedup": f"{finding['_id']}|{prop['rule']}|{prop['target']}", "finding_id": str(finding["_id"]), "dedup_key": finding["dedup_key"],
           "check_id": finding["check_id"], "mode": cfg.get("mode"), "at": now.isoformat(), "actor": "security_agent", **prop,
           "expires_at": (now + timedelta(minutes=prop["expires_min"])).isoformat() if prop.get("expires_min") else None}
    shared = await shared_ip_share(db, prop["target"]) if prop["action"] == "block_ip" else 0.0
    if prop.get("blocked_by") or rules.is_protected(cfg, prop["target_kind"], prop["target"]):
        row["status"] = "refused_protected"
        await open_or_update(db, f("protected_target", f"{prop['rule']}:{prop['target']}", "critical", "platform",
                                   f"{prop['rule']} would have applied {prop['action']} to protected {prop['target_kind']} {prop['target']} — refused",
                                   {"rule": prop["rule"], "target": prop["target"], "finding": finding["dedup_key"]}))
    elif shared >= rules.SHARED_IP_MIN_SHARE:
        # S1 — one address carrying most of the traffic is a proxy / NAT in front of
        # every user (Caddy, cPanel, corporate NAT): blocking it locks everyone out.
        row["status"] = "refused_shared_ip"
        row["traffic_share"] = round(shared, 3)
        await open_or_update(db, f("proxy_collapse_suspected", f"ip:{prop['target']}", "critical", "platform",
                                   f"{prop['rule']} wanted to block {prop['target']}, which carries {shared:.0%} of recent auth/bridge traffic — "
                                   "refused (a proxy is probably hiding client IPs; fix the ingress real-IP chain)",
                                   {"ip": prop["target"], "share": round(shared, 3)}))
    elif cfg.get("mode") != "enforce":
        row["status"] = "would_have_done"
    elif prop["rule"] not in (cfg.get("rules_enabled") or []):
        row["status"] = "would_have_done"
        row["note"] = f"{prop['rule']} not enabled yet; " + str(prop.get("note") or "")
    else:
        n_hour = await db.security_actions.count_documents({"kind": "containment", "status": "done", "at": {"$gte": since}})
        cap = int(cfg.get("max_actions_per_hour") or 10)
        if n_hour >= cap:
            row["status"] = "refused_cap"
            await open_or_update(db, f("containment_cap_reached", "platform", "critical", "platform",
                                       f"{n_hour} containment actions in the last hour reached the cap of {cap}; agent is alert-only",
                                       {"actions_last_hour": n_hour, "cap": cap}))
        else:
            row.update(await _apply(db, cfg, prop, row))
    res = await db.security_actions.insert_one(dict(row))
    row["_id"] = res.inserted_id
    if row.get("block_id"):
        await db.security_blocks.update_one({"_id": _oid(row["block_id"])}, {"$set": {"action_id": str(res.inserted_id)}})
    if row["status"] == "done":
        await db.security_findings.update_one({"_id": finding["_id"], "status": "open"}, {"$set": {
            "status": "contained", "fixed": "contained", "action_taken": f"{prop['rule']} {prop['action']} {prop['target']} (automatic) [action {res.inserted_id}]"}})
        await _audit(db, f"security_action_{prop['action']}", prop["target"], {"rule": prop["rule"], "action_id": str(res.inserted_id), "finding": finding["dedup_key"],
                                                                              "expires_at": row.get("expires_at"), "evidence": mask(str(finding.get("what_happened")))[:300]})
    return row


async def shared_ip_share(db, ip: str) -> float:
    """S1 — share of the last 10 min of IP-keyed security events / login attempts carried by `ip`."""
    try:
        since = (_now() - timedelta(minutes=10)).isoformat()
        total = await db.security_events.count_documents({"at": {"$gte": since}, "ip": {"$nin": [None, ""]}})
        if total < rules.SHARED_IP_MIN_EVENTS:
            return 0.0
        mine = await db.security_events.count_documents({"at": {"$gte": since}, "ip": str(ip)})
        return mine / total
    except Exception as e:  # noqa: BLE001
        log.warning("shared-ip share unavailable: %s", type(e).__name__)
        return 0.0


async def _freeze_account(db, acc: dict, prop: dict, row: dict) -> dict:
    prior = {"trading_authority": acc.get("trading_authority"), "authority_lock": acc.get("authority_lock")}
    # S8 — the agent freeze lives in its OWN field so a PANIC and its release never lift it
    freeze = {"rule": prop["rule"], "by": "security_agent", "at": _now().isoformat(), "dedup_key": row["dedup_key"]}
    sets = {"security_freeze": freeze}
    if acc.get("trading_authority") in (None, "FULL"):
        sets.update({"trading_authority": "CLOSE_ONLY", "authority_lock": {"reason": "security_agent", **freeze}})
        note = None
    else:
        note = "account already restricted — authority left unchanged (never overrides a PANIC lock); freeze recorded"
    await db.accounts.update_one({"_id": acc["_id"]}, {"$set": sets})
    await _notify_owner(db, acc.get("user_id"), "freeze_new_entries", acc.get("label") or str(acc["_id"]), 0)
    return {"status": "done", "prior": prior, **({"note": note} if note else {})}


async def _apply(db, cfg: dict, prop: dict, row: dict) -> dict:
    """Mutations per action type; returns fields to merge (status, prior, block_id…)."""
    act, target, minutes = prop["action"], prop["target"], int(prop.get("expires_min") or 0)
    if act in BLOCK_ACTIONS:
        kind = BLOCK_ACTIONS[act]
        exp = _now() + timedelta(minutes=minutes or int(cfg.get("ip_block_min") or 60))
        res = await db.security_blocks.insert_one({"kind": kind, "value": target, "scope": prop.get("scope", "all"), "active": True, "rule": prop["rule"],
                                                   "created_at": _now().isoformat(), "expires_at": exp, "actor": "security_agent", "dedup_key": row["dedup_key"]})
        if act == "lock_login":
            await _notify_owner(db, await _owner_of_email(db, target), act, target, minutes)
        if act == "lock_otp":
            await _notify_owner(db, await _owner_of_email(db, target), act, target, minutes)
        return {"status": "done", "block_id": str(res.inserted_id), "expires_at": exp.isoformat()}
    if act == "revoke_sessions":
        from security import revoke_all_user_sessions
        n = await revoke_all_user_sessions(db, str(target), f"security_agent:{prop['rule']}")
        return {"status": "done", "revoked_sessions": n, "undo": None}
    if act == "suspend_bridge_token":
        acc = await db.accounts.find_one({"_id": _oid(target)}, {"bridge_token": 1, "user_id": 1, "label": 1})
        if not acc or not acc.get("bridge_token"):
            return {"status": "skipped", "note": "account or token not found"}
        exp = (_now() + timedelta(minutes=minutes)).isoformat() if minutes else None
        await db.accounts.update_one({"_id": acc["_id"]}, {"$set": {"bridge_token_suspended": {
            "token": acc["bridge_token"], "at": _now().isoformat(), "rule": prop["rule"], "dedup_key": row["dedup_key"],
            "expires_at": exp}}})
        await _notify_owner(db, acc.get("user_id"), act, acc.get("label") or target, 0)
        return {"status": "done", "prior": {"bridge_token_suspended": None}}
    if act == "freeze_new_entries":
        proj = {"trading_authority": 1, "authority_lock": 1, "user_id": 1, "label": 1}
        if prop.get("target_kind") == "user":
            # S4 — R7 on an admin/user actor: revoke the actor's sessions and freeze
            # EVERY live account of that user (a user id is not an account id).
            out = {"status": "done", "accounts": [], "priors": {}}
            if prop.get("revoke_sessions"):
                from security import revoke_all_user_sessions
                out["revoked_sessions"] = await revoke_all_user_sessions(db, str(target), f"security_agent:{prop['rule']}")
            async for acc in db.accounts.find({"user_id": str(target), "mode": {"$ne": "paper"}}, proj).limit(100):
                r = await _freeze_account(db, acc, prop, row)
                out["accounts"].append(str(acc["_id"]))
                out["priors"][str(acc["_id"])] = r["prior"]
            if not out["accounts"]:
                out["note"] = "user has no live accounts — sessions revoked only"
            return out
        acc = await db.accounts.find_one({"_id": _oid(target)}, proj)
        if not acc:
            return {"status": "skipped", "note": "account not found"}
        return await _freeze_account(db, acc, prop, row)
    return {"status": "skipped", "note": f"unknown action {act}"}


async def undo(db, action_id, actor: str, note: str = "") -> dict:
    """Restores the exact prior state; idempotent; audited. Returns the updated action row."""
    row = await db.security_actions.find_one({"_id": _oid(action_id), "kind": "containment"})
    if not row:
        raise LookupError("action not found")
    if row.get("status") != "done":
        raise ValueError(f"action is {row.get('status')} — nothing to undo")
    act = row["action"]
    if act in BLOCK_ACTIONS and row.get("block_id"):
        await db.security_blocks.update_one({"_id": _oid(row["block_id"])}, {"$set": {"active": False, "undone_at": _now().isoformat(), "undone_by": actor}})
    elif act == "suspend_bridge_token":
        await db.accounts.update_one({"_id": _oid(row["target"]), "bridge_token_suspended.dedup_key": row["dedup_key"]}, {"$unset": {"bridge_token_suspended": ""}})
    elif act == "freeze_new_entries":
        priors = row.get("priors") or {str(row["target"]): row.get("prior") or {}}
        for aid, prior in priors.items():
            await _unfreeze_account(db, aid, prior or {})
    elif act == "revoke_sessions":
        raise ValueError("revoked sessions cannot be restored — the user signs in again")
    await db.security_actions.update_one({"_id": row["_id"]}, {"$set": {"status": "undone", "undone_at": _now().isoformat(), "undone_by": actor, "undo_note": mask(note)[:300]}})
    await db.security_findings.update_one({"_id": _oid(row["finding_id"]), "status": "contained"}, {"$set": {"status": "open", "fixed": "no", "action_taken": f"undone by {actor}"}})
    await _audit(db, f"security_undo_{act}", row["target"], {"action_id": str(row["_id"]), "note": mask(note)[:200]}, actor=actor)
    return await db.security_actions.find_one({"_id": row["_id"]})


async def _unfreeze_account(db, account_id, prior: dict) -> None:
    """S16 — restore the EXACT prior authority (unset when it was unset), never a hard-coded FULL."""
    sets, unsets = {}, {"security_freeze": ""}
    for k in ("trading_authority", "authority_lock"):
        if prior.get(k) is None:
            unsets[k] = ""
        else:
            sets[k] = prior[k]
    upd = {"$unset": unsets, **({"$set": sets} if sets else {})}
    # only touch the authority fields if they are still the agent's own lock
    res = await db.accounts.update_one({"_id": _oid(account_id), "authority_lock.reason": "security_agent"}, upd)
    if not res.matched_count:
        await db.accounts.update_one({"_id": _oid(account_id)}, {"$unset": {"security_freeze": ""}})


async def expire_finished(db) -> int:
    """S10 — a contained finding whose block has expired is resolved (it is no longer active);
    test alerts / protected-target / cap-reached findings expire after an hour so the Critical
    banner and 30-min repeats do not run forever (S16)."""
    now = _now()
    n = 0
    async for a in db.security_actions.find({"kind": "containment", "status": "done", "expires_at": {"$lt": now.isoformat(), "$ne": None}}).limit(200):
        await db.security_actions.update_one({"_id": a["_id"]}, {"$set": {"status": "expired", "expired_at": now.isoformat()}})
        if a.get("action") == "suspend_bridge_token":
            await db.accounts.update_one({"_id": _oid(a["target"]), "bridge_token_suspended.dedup_key": a["dedup_key"]}, {"$unset": {"bridge_token_suspended": ""}})
        r = await db.security_findings.update_one({"_id": _oid(a["finding_id"]), "status": "contained"}, {"$set": {
            "status": "resolved", "resolved_at": now.isoformat(), "resolved_by": "security_agent", "fixed": "yes", "status_note": "containment expired"}})
        n += r.modified_count
    cut = (now - timedelta(hours=1)).isoformat()
    r = await db.security_findings.update_many(
        {"check_id": {"$in": ["test_alert", "protected_target", "containment_cap_reached", "proxy_collapse_suspected"]},
         "status": {"$in": ["open", "acknowledged"]}, "last_seen": {"$lt": cut}},
        {"$set": {"status": "resolved", "resolved_at": now.isoformat(), "resolved_by": "security_agent", "fixed": "yes",
                  "status_note": "informational finding expired after 1 h"}})
    return n + r.modified_count


async def extend(db, action_id, minutes: int, actor: str) -> dict:
    """Admins can lengthen or shorten a block (minutes from now; 0 = expire now)."""
    row = await db.security_actions.find_one({"_id": _oid(action_id), "kind": "containment", "status": "done"})
    if not row or not row.get("block_id"):
        raise LookupError("no active block for this action")
    exp = _now() + timedelta(minutes=max(0, int(minutes)))
    await db.security_blocks.update_one({"_id": _oid(row["block_id"])}, {"$set": {"expires_at": exp, "extended_by": actor}})
    await db.security_actions.update_one({"_id": row["_id"]}, {"$set": {"expires_at": exp.isoformat()}})
    await _audit(db, "security_extend_block", row["target"], {"action_id": str(row["_id"]), "expires_at": exp.isoformat()}, actor=actor)
    return await db.security_actions.find_one({"_id": row["_id"]})


async def sweep(db, cfg: dict) -> int:
    """Every tick: evaluate rules on open findings, execute (or dry-run) once per finding+rule+target per hour."""
    since = (_now() - timedelta(hours=1)).isoformat()
    await expire_finished(db)
    checks = sorted({c for r in rules.RULES.values() for c in r["checks"]})
    rows = await db.security_findings.find({"status": {"$in": ["open", "contained"]}, "check_id": {"$in": checks}}).limit(500).to_list(length=500)
    n = 0
    for fd in rows:
        for prop in rules.evaluate(fd, cfg):
            dedup = f"{fd['_id']}|{prop['rule']}|{prop['target']}"
            if await db.security_actions.find_one({"dedup": dedup, "at": {"$gte": since}}):
                continue
            if await db.security_actions.find_one({"dedup": dedup, "status": "done"}):
                continue                                           # already contained for this finding; expiry handles the rest
            await execute(db, cfg, prop, fd)
            n += 1
    return n
