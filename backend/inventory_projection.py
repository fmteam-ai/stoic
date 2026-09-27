"""Inventory projection (audit round 9 P0-02) — ONE account/bot inventory keyed
by immutable account id + broker environment.

Counts are NEVER collapsed: expected · configured · enabled · connected · fresh
· tradable · bots_enabled are reported separately. Live readiness is satisfied
only by LIVE accounts (demo/paper never count). Enabled account ⇔ enabled bot
is enforced one-to-one. The inventory hash must equal the last APPROVED
configuration event (db.inventory_config_events) or promotion/new entries block.
"""
import hashlib
import json
from datetime import datetime, timezone

LIVE_ENVS = {"live", "real"}


def production_mode() -> bool:
    import os
    return os.environ.get("APP_ENV", "").strip().lower() == "production"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def inventory_hash(rows: list) -> str:
    key = sorted((r["account_id"], r["environment"], bool(r["enabled"]), bool(r["bot_enabled"])) for r in rows)
    return hashlib.sha256(json.dumps(key, separators=(",", ":")).encode()).hexdigest()


async def projection(db, scope_user_id: str | None = None) -> dict:
    from state_contract import effective_connection_state, account_truth
    q = {"user_id": scope_user_id} if scope_user_id else {}
    exp = await db.platform_state.find_one({"_id": "inventory_expectation"}) or {}
    bots = {}
    async for b in db.bot_configs.find({**q, "account_id": {"$ne": None}}, {"account_id": 1, "active": 1}):
        bots[str(b["account_id"])] = bool(b.get("active"))
    rows, violations = [], []
    counts = {"expected_accounts": exp.get("accounts"), "expected_enabled": exp.get("enabled"),
              "expected_bots": exp.get("bots"), "configured": 0, "live_configured": 0, "enabled": 0,
              "live_enabled": 0, "bots_enabled": 0, "connected": 0, "fresh": 0, "tradable": 0}
    async for acc in db.accounts.find(q):
        aid = str(acc["_id"])
        env = str(acc.get("mode") or "demo").lower()
        enabled = acc.get("trading_enabled") is True
        bot_on = bots.get(aid, False)
        conn = effective_connection_state(acc)
        open_local = await db.trades.count_documents({"account_id": aid, "status": "open"})
        truth = account_truth(acc, open_local)
        fresh = truth["position_truth"] == "FRESH"
        tradable = enabled and bot_on and conn["connected"] and fresh and truth["execution_authority"] == "FULL"
        vid = acc.get("verified_identity") or {}
        rows.append({"account_id": aid,                                   # platform-owned immutable UUID
                     "broker_identity": {"login": acc.get("account_login") or acc.get("account_number"),
                                         "server": vid.get("broker_server") or acc.get("server"),
                                         "verified": bool(vid.get("account_number"))},
                     "label": acc.get("label"), "environment": env, "enabled": enabled, "bot_enabled": bot_on,
                     "connection_state": conn["state"], "position_truth": truth["position_truth"],
                     "tradable": tradable, "reconciliation_seq": int(acc.get("reconciliation_seq") or 0),
                     "last_reconciled_at": acc.get("last_reconciled_at")})
        counts["configured"] += 1
        counts["live_configured"] += env in LIVE_ENVS
        counts["enabled"] += enabled
        counts["live_enabled"] += enabled and env in LIVE_ENVS
        counts["bots_enabled"] += bot_on
        counts["connected"] += conn["connected"]
        counts["fresh"] += fresh
        counts["tradable"] += tradable
        if enabled != bot_on:
            violations.append(f"{acc.get('label') or aid}: enabled account/bot relation broken "
                              f"(account_enabled={enabled}, bot_enabled={bot_on})")
    if exp:
        ids = set(exp.get("account_ids") or [])
        if exp.get("accounts") is not None and counts["configured"] != exp["accounts"]:
            violations.append(f"configured accounts {counts['configured']} != expected {exp['accounts']}")
        if exp.get("enabled") is not None and counts["live_enabled"] != exp["enabled"]:
            violations.append(f"enabled LIVE accounts {counts['live_enabled']} != expected {exp['enabled']} "
                              "(demo/paper never satisfy a live requirement)")
        if exp.get("bots") is not None and counts["bots_enabled"] != exp["bots"]:
            violations.append(f"enabled bots {counts['bots_enabled']} != expected {exp['bots']}")
        if ids:
            enabled_ids = {r["account_id"] for r in rows if r["enabled"]}
            if enabled_ids != ids:
                violations.append("enabled account ids differ from the approved set")
    h = inventory_hash(rows)
    approved = await db.inventory_config_events.find_one({"approved": True}, sort=[("at", -1)])
    unapproved_change = bool(approved) and approved.get("inventory_hash") != h
    if unapproved_change:
        violations.append("inventory changed without an approved configuration event")
    prod = production_mode()
    if prod and not exp:
        violations.append("no inventory expectation declared — production fails closed")
    if prod and not approved:
        violations.append("no approved inventory configuration event — production fails closed")
    return {"as_of": _now(), "scope_user_id": scope_user_id, "counts": counts, "accounts": rows,
            "inventory_hash": h, "approved_hash": (approved or {}).get("inventory_hash"),
            "approved_at": (approved or {}).get("at"), "unapproved_change": unapproved_change,
            "violations": violations, "blocking": bool(violations) and (prod or bool(exp or approved)),
            "note": "Canonical inventory projection — counts are never collapsed."}


async def approve_current(db, actor_email: str, note: str, scope_user_id: str | None = None) -> dict:
    """Record an approved configuration event for the CURRENT inventory."""
    from audit_chain import append_chained
    proj = await projection(db, scope_user_id)
    ev = {"inventory_hash": proj["inventory_hash"], "approved": True, "actor": actor_email,
          "note": (note or "")[:500], "at": _now(), "counts": proj["counts"],
          "account_ids": [r["account_id"] for r in proj["accounts"] if r["enabled"]]}
    await db.inventory_config_events.insert_one(dict(ev))
    await append_chained(db, {"actor_email": actor_email, "action": "inventory_config_approved",
                              "target_kind": "platform", "target_id": "inventory", "reason": ev["note"],
                              "at": ev["at"], "meta": {"inventory_hash": ev["inventory_hash"], "counts": ev["counts"]}})
    return await projection(db, scope_user_id)


async def propose_expectation(db, payload: dict, actor_email: str) -> dict:
    """Two-admin control (round 10 P1-01): one admin PROPOSES the desired state,
    a DIFFERENT admin (step-up verified) must approve before it takes effect."""
    doc = {"_id": "inventory_expectation_pending", "accounts": int(payload["accounts"]),
           "enabled": int(payload["enabled"]), "bots": int(payload["bots"]),
           "account_ids": list(payload.get("account_ids") or []), "scope_user_id": payload.get("scope_user_id"),
           "proposed_by": actor_email, "proposed_at": _now()}
    await db.platform_state.replace_one({"_id": "inventory_expectation_pending"}, doc, upsert=True)
    return doc


async def approve_expectation(db, approver_email: str) -> dict:
    from fastapi import HTTPException
    from audit_chain import append_chained
    pending = await db.platform_state.find_one({"_id": "inventory_expectation_pending"})
    if not pending:
        raise HTTPException(status_code=404, detail={"code": "no_pending_expectation"})
    if pending.get("proposed_by", "").lower() == approver_email.lower():
        raise HTTPException(status_code=403, detail={"code": "second_admin_required",
                                                     "message": "the proposing admin cannot approve their own expectation"})
    doc = {k: pending[k] for k in ("accounts", "enabled", "bots", "account_ids", "scope_user_id", "proposed_by", "proposed_at")}
    doc.update(_id="inventory_expectation", approved_by=approver_email, set_at=_now())
    await db.platform_state.replace_one({"_id": "inventory_expectation"}, doc, upsert=True)
    await db.platform_state.delete_one({"_id": "inventory_expectation_pending"})
    await append_chained(db, {"actor_email": approver_email, "action": "inventory_expectation_approved",
                              "target_kind": "platform", "target_id": "inventory_expectation",
                              "reason": f"proposed by {pending['proposed_by']}", "at": doc["set_at"],
                              "meta": {k: doc[k] for k in ("accounts", "enabled", "bots", "account_ids")}})
    return doc


async def set_expectation(db, payload: dict, actor_email: str) -> dict:
    """Direct set — tests/seed only (non-production). Production goes through propose→approve."""
    if production_mode():
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail={"code": "two_admin_required"})
    doc = {"_id": "inventory_expectation", "accounts": int(payload["accounts"]), "enabled": int(payload["enabled"]),
           "bots": int(payload["bots"]), "account_ids": list(payload.get("account_ids") or []),
           "scope_user_id": payload.get("scope_user_id"), "set_by": actor_email, "set_at": _now()}
    await db.platform_state.replace_one({"_id": "inventory_expectation"}, doc, upsert=True)
    return doc
