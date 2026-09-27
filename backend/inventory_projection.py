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
    from app_env import is_production      # SEC-001: one definition of "production" ("production" | "prod")
    return is_production()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def inventory_hash(rows: list) -> str:
    key = sorted((r["account_id"], r["environment"], bool(r["enabled"]), bool(r["bot_enabled"])) for r in rows)
    return hashlib.sha256(json.dumps(key, separators=(",", ":")).encode()).hexdigest()


async def projection(db, scope_user_id: str | None = None, *, include_foreign_bots: bool = True) -> dict:
    """Bots are matched by ACCOUNT id across all tenants (a bot row from another
    user pointing at one of our accounts is a duplicate, not invisible)."""
    from state_contract import effective_connection_state, account_truth
    q = {"user_id": scope_user_id} if scope_user_id else {}
    exp = await db.platform_state.find_one({"_id": "inventory_expectation"}) or {}
    # round 11 P1-01 — aggregate EVERY bot row; duplicates/orphans/unknown ids are defects
    bot_rows: dict = {}
    raw_bots = {"configured": 0, "active": 0, "null_account": 0}
    account_ids_scope = [str(a["_id"]) async for a in db.accounts.find(q, {"_id": 1})]
    bot_q = ({"$or": [q, {"account_id": {"$in": account_ids_scope}}]} if (q and include_foreign_bots) else q)
    async for b in db.bot_configs.find(bot_q, {"account_id": 1, "active": 1}):
        raw_bots["configured"] += 1
        raw_bots["active"] += bool(b.get("active"))
        aid_b = b.get("account_id")
        if not aid_b:
            raw_bots["null_account"] += 1
            continue
        bot_rows.setdefault(str(aid_b), []).append(bool(b.get("active")))
    account_ids_seen: set = set()
    rows, violations = [], []
    counts = {"expected_accounts": exp.get("accounts"), "expected_enabled": exp.get("enabled"),
              "expected_bots": exp.get("bots"), "configured": 0, "live_configured": 0, "enabled": 0,
              "live_enabled": 0, "bots_enabled": 0, "connected": 0, "fresh": 0, "tradable": 0,
              "bots_configured_raw": raw_bots["configured"], "bots_active_raw": raw_bots["active"],
              "bots_null_account": raw_bots["null_account"], "bots_duplicate": 0, "bots_orphan": 0}
    async for acc in db.accounts.find(q):
        aid = str(acc["_id"])
        account_ids_seen.add(aid)
        acc_bots = bot_rows.get(aid, [])
        if len(acc_bots) > 1:
            counts["bots_duplicate"] += len(acc_bots) - 1
            violations.append(f"{acc.get('label') or aid}: {len(acc_bots)} bot configurations (exactly one required)")
        env = str(acc.get("mode") or "demo").lower()
        enabled = acc.get("trading_enabled") is True
        bot_on = any(acc_bots)
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
    structural = list(violations)          # duplicates / broken 1:1 so far — always blocking
    orphans = [a for a in bot_rows if a not in account_ids_seen]
    if orphans:
        counts["bots_orphan"] = len(orphans)
        violations.append(f"{len(orphans)} bot configuration(s) reference unknown accounts")
    if raw_bots["null_account"]:
        violations.append(f"{raw_bots['null_account']} bot configuration(s) with no account id")
    structural = list(violations)
    if exp:
        ids = set(exp.get("account_ids") or [])
        if ids:
            outside = [r for r in rows if r["bot_enabled"] and r["account_id"] not in ids]
            if outside:
                violations.append(f"{len(outside)} active bot(s) outside the approved account set")
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
            "structural_defects": structural,
            "violations": violations,
            "blocking": bool(structural) or (bool(violations) and (prod or bool(exp or approved))),
            "note": "Canonical inventory projection — counts are never collapsed."}


async def approve_current(db, actor_email: str, note: str, scope_user_id: str | None = None) -> dict:
    """Record an approved configuration event for the CURRENT inventory."""
    from audit_chain import append_chained
    from fastapi import HTTPException
    proj = await projection(db, scope_user_id)
    real = [v for v in proj["violations"] if "approved configuration event" not in v]
    if real:
        raise HTTPException(status_code=409, detail={"code": "inventory_not_approvable", "violations": real})
    ev = {"inventory_hash": proj["inventory_hash"], "approved": True, "actor": actor_email,
          "note": (note or "")[:500], "at": _now(), "counts": proj["counts"],
          "account_ids": [r["account_id"] for r in proj["accounts"] if r["enabled"]]}
    await db.inventory_config_events.insert_one(dict(ev))
    await append_chained(db, {"actor_email": actor_email, "action": "inventory_config_approved",
                              "target_kind": "platform", "target_id": "inventory", "reason": ev["note"],
                              "at": ev["at"], "meta": {"inventory_hash": ev["inventory_hash"], "counts": ev["counts"]}})
    return await projection(db, scope_user_id)


DEPLOYMENT_POLICY = {"accounts": 6, "enabled": 3, "bots": 3}       # signed installation policy (round 11 P1-02)


def validate_expectation(payload: dict, *, require_policy: bool) -> dict:
    """Set-relationship validation shared by proposal AND approval (no TOCTOU drift)."""
    from fastapi import HTTPException
    errors = []
    try:
        acc, en, bots = int(payload["accounts"]), int(payload["enabled"]), int(payload["bots"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=400, detail={"code": "expectation_invalid", "errors": ["accounts/enabled/bots (int) required"]})
    ids = list(payload.get("account_ids") or [])
    if min(acc, en, bots) < 0:
        errors.append("counts must be non-negative")
    if en > acc:
        errors.append("enabled must not exceed accounts")
    if bots != en:
        errors.append("bots must equal enabled (one bot per enabled account)")
    if len(ids) != len(set(ids)):
        errors.append("account_ids must be unique")
    if ids and len(ids) != en:
        errors.append("len(account_ids) must equal enabled")
    if require_policy and (acc, en, bots) != (DEPLOYMENT_POLICY["accounts"], DEPLOYMENT_POLICY["enabled"], DEPLOYMENT_POLICY["bots"]) \
            and not _policy_migration_signed(payload):
        errors.append("production target must be 6/3/3 unless a signed policy migration is supplied")
    if require_policy and not ids:
        errors.append("production expectation must name the approved account ids")
    if errors:
        raise HTTPException(status_code=400, detail={"code": "expectation_invalid", "errors": errors})
    return {"accounts": acc, "enabled": en, "bots": bots, "account_ids": ids}


def _policy_migration_signed(payload: dict) -> bool:
    """A policy migration is an Ed25519-signed statement over the new target by the release key."""
    import json as _json
    mig = payload.get("policy_migration") or {}
    sig = mig.get("signature_hex")
    if not sig:
        return False
    try:
        from release_signing import verify_hex
        body = _json.dumps({"accounts": int(payload["accounts"]), "enabled": int(payload["enabled"]),
                            "bots": int(payload["bots"]), "migration_id": mig.get("migration_id")},
                           sort_keys=True, separators=(",", ":")).encode()
        return verify_hex(body, sig)
    except Exception:  # noqa: BLE001
        return False


async def propose_expectation(db, payload: dict, actor_email: str) -> dict:
    """Two-admin control (round 10 P1-01): one admin PROPOSES the desired state,
    a DIFFERENT admin (step-up verified) must approve before it takes effect."""
    v = validate_expectation(payload, require_policy=production_mode())
    doc = {"_id": "inventory_expectation_pending", **v, "scope_user_id": payload.get("scope_user_id"),
           "policy_migration": payload.get("policy_migration"),
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
    validate_expectation(pending, require_policy=production_mode())            # re-validated at approval
    doc = {k: pending.get(k) for k in ("accounts", "enabled", "bots", "account_ids", "scope_user_id", "proposed_by", "proposed_at")}
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
