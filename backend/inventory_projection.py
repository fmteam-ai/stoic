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
import os
from datetime import datetime, timedelta, timezone

from pymongo.errors import DuplicateKeyError

LIVE_ENVS = {"live", "real"}


def production_mode() -> bool:
    from app_env import is_production      # SEC-001: one definition of "production" ("production" | "prod")
    return is_production()


def approval_mode() -> str:
    """four_eyes (default: proposer ≠ approver) | single_admin (explicit operator choice:
    one admin may approve their own proposal after a fresh step-up; every event is stamped)."""
    return "single_admin" if os.environ.get("INVENTORY_APPROVAL_MODE", "").strip().lower() == "single_admin" else "four_eyes"


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
    # one grouped query for open positions (was one count per account — N+1 that
    # stalled the public status probe on large tenants / remote Atlas)
    open_by_account: dict = {}
    open_match = {"status": "open"}
    if q:
        open_match["account_id"] = {"$in": account_ids_scope}
    async for g in db.trades.aggregate([{"$match": open_match},
                                        {"$group": {"_id": "$account_id", "n": {"$sum": 1}}}]):
        open_by_account[str(g["_id"])] = int(g["n"])
    docs = []
    async for acc in db.accounts.find(q):
        docs.append(acc)
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
        open_local = open_by_account.get(aid, 0)
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
        expiry = policy_expiry(exp)
        if expiry["expired"]:
            violations.append(f"signed policy {exp.get('policy_version')} expired {expiry['expires_at'][:10]} — inventory close-only until a new signed policy is approved")   # A16-4
        ids = set(exp.get("account_ids") or [])
        if ids:
            outside = [r for r in rows if r["bot_enabled"] and r["account_id"] not in ids]
            if outside:
                violations.append(f"{len(outside)} active bot(s) outside the approved account set")
        if exp.get("accounts") is not None and counts["configured"] != exp["accounts"]:
            violations.append(f"configured accounts {counts['configured']} != expected {exp['accounts']}")
        if exp.get("demo_only"):
            # A15-1 — signed DEMO-only policy: every listed account must be an attested DEMO account and enabled
            from broker_env import attested_environment
            if exp.get("enabled") is not None and counts["enabled"] != exp["enabled"]:
                violations.append(f"enabled accounts {counts['enabled']} != expected {exp['enabled']} (demo-only policy)")
            not_demo = [a.get("label") or str(a["_id"]) for a in docs
                        if str(a["_id"]) in ids and attested_environment(a) != "DEMO"]
            if not_demo:
                violations.append("demo-only policy but account(s) not attested DEMO: " + ", ".join(not_demo[:3]))
        elif exp.get("enabled") is not None and counts["live_enabled"] != exp["enabled"]:
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
            "violations": violations, "approval_mode": approval_mode(),
            "approved_mode": (approved or {}).get("approval_mode"),
            "blocking": bool(structural) or (bool(violations) and (prod or bool(exp or approved))),
            "note": "Canonical inventory projection — counts are never collapsed."}


HASH_PENDING_ID = "inventory_hash_pending"
HASH_PROPOSAL_TTL_MIN = 30


async def approve_current(db, actor_email: str, note: str, scope_user_id: str | None = None) -> dict:
    """Step 1 of the two-admin inventory-hash approval (round 12 P2-05): the
    proposing admin binds the CURRENT hash + exact enabled account ids + expiry.
    Nothing is approved until a DIFFERENT step-up admin confirms (confirm_current)."""
    from fastapi import HTTPException
    proj = await projection(db, scope_user_id)
    real = [v for v in proj["violations"] if "approved configuration event" not in v]
    if real:
        raise HTTPException(status_code=409, detail={"code": "inventory_not_approvable", "violations": real})
    now = datetime.now(timezone.utc)
    pending = {"_id": HASH_PENDING_ID, "inventory_hash": proj["inventory_hash"],
               "account_ids": sorted(r["account_id"] for r in proj["accounts"] if r["enabled"]),
               "counts": proj["counts"], "scope_user_id": scope_user_id, "proposed_by": actor_email.lower(),
               "note": (note or "")[:500], "proposed_at": _now(),
               "expires_at": (now + timedelta(minutes=HASH_PROPOSAL_TTL_MIN)).isoformat()}
    await db.platform_state.replace_one({"_id": HASH_PENDING_ID}, pending, upsert=True)
    return {**proj, "pending_approval": {k: pending[k] for k in ("inventory_hash", "account_ids", "proposed_by",
                                                                   "proposed_at", "expires_at")}}


async def pending_hash_approval(db) -> dict | None:
    p = await db.platform_state.find_one({"_id": HASH_PENDING_ID})
    if not p:
        return None
    try:
        if datetime.fromisoformat(p["expires_at"]) <= datetime.now(timezone.utc):
            await db.platform_state.delete_one({"_id": HASH_PENDING_ID})
            return None
    except (KeyError, ValueError, TypeError):
        await db.platform_state.delete_one({"_id": HASH_PENDING_ID})
        return None
    return p


async def confirm_current(db, approver_email: str) -> dict:
    """Step 2: a different admin confirms; the hash and enabled ids are RECOMPUTED
    immediately before commit and must equal the proposal exactly."""
    from fastapi import HTTPException
    from audit_chain import append_chained
    pending = await pending_hash_approval(db)
    if not pending:
        raise HTTPException(status_code=404, detail={"code": "no_pending_inventory_approval"})
    if approval_mode() == "four_eyes" and pending["proposed_by"] == approver_email.lower():
        raise HTTPException(status_code=403, detail={"code": "second_admin_required",
                                                     "message": "the proposing admin cannot confirm their own proposal"})
    proj = await projection(db, pending.get("scope_user_id"))
    ids = sorted(r["account_id"] for r in proj["accounts"] if r["enabled"])
    real = [v for v in proj["violations"] if "approved configuration event" not in v]
    if real or proj["inventory_hash"] != pending["inventory_hash"] or ids != pending["account_ids"]:
        await db.platform_state.delete_one({"_id": HASH_PENDING_ID})
        raise HTTPException(status_code=409, detail={"code": "inventory_changed_since_proposal",
                                                     "violations": real, "proposal_invalidated": True})
    ev = {"inventory_hash": proj["inventory_hash"], "approved": True, "actor": pending["proposed_by"],
          "approved_by": approver_email.lower(), "note": pending["note"], "at": _now(), "counts": proj["counts"],
          "account_ids": ids, "approval_mode": approval_mode()}
    await db.inventory_config_events.insert_one(dict(ev))
    await db.platform_state.delete_one({"_id": HASH_PENDING_ID})
    await append_chained(db, {"actor_email": approver_email, "action": "inventory_config_approved",
                              "target_kind": "platform", "target_id": "inventory", "reason": ev["note"],
                              "at": ev["at"], "meta": {"inventory_hash": ev["inventory_hash"], "counts": ev["counts"],
                                                       "proposed_by": pending["proposed_by"], "account_ids": ids,
                                                       "approval_mode": ev["approval_mode"]}})
    from canonical_decision import bump_authority_version
    await bump_authority_version(db, "inventory_approved")
    return await projection(db, pending.get("scope_user_id"))


DEPLOYMENT_POLICY = {"accounts": 6, "enabled": 3, "bots": 3}       # signed installation policy (round 11 P1-02)
DEPLOYMENT_POLICY_VERSION = "6/3/3-v1"
MIGRATION_SCHEMA = "stoic.policy-migration/v3"   # v3 (A15-1): signed `demo_only` flag
# A16-3 — ONE validation rule per field, shared by the workflow (policy-migration.yml), the signer script
# and the server. `policy_version` becomes a file name → no "/"; the previous version keeps "/" (6/3/3-v1).
POLICY_VERSION_RE = r"[A-Za-z0-9._-]{1,64}"
PREVIOUS_POLICY_VERSION_RE = r"[A-Za-z0-9._/-]{1,64}"
# A16-4 — demo policies are short-lived: 30 days by default, never more than 45
DEMO_POLICY_DEFAULT_DAYS = 30
DEMO_POLICY_MAX_DAYS = 45
POLICY_MAX_DAYS = 180              # SA7-P3 — no policy, demo or live, is valid for more than half a year
POLICY_EXPIRY_REMINDER_DAYS = 3


def installation_id() -> str | None:
    """Installation identity a signed migration must name (production: mandatory)."""
    v = (os.environ.get("STOIC_INSTALLATION_ID") or "").strip()
    if v:
        return v
    return None if production_mode() else "dev-local"


def environment_label() -> str:
    return "production" if production_mode() else "preview"


def migration_body(mig: dict) -> bytes:
    """Canonical signed statement of the COMPLETE policy transition (round 12 P2-06)."""
    fields = ("schema", "installation_id", "environment", "previous_policy_version", "policy_version",
              "accounts", "enabled", "bots", "account_ids", "demo_only", "reason", "issuer", "issued_at", "expires_at", "nonce")
    body = {k: mig.get(k) for k in fields}
    body["account_ids"] = sorted(body.get("account_ids") or [])
    body["demo_only"] = bool(body.get("demo_only"))     # A15-1 — DEMO-only policies are signed as such
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def policy_expiry(exp: dict, now: datetime | None = None) -> dict:
    """A16-4 — {'expires_at', 'expired', 'days_left', 'reminder_due'} for the approved expectation
    (all None/False when the policy has no lifetime, i.e. the built-in deployment policy)."""
    raw = (exp or {}).get("policy_expires_at")
    if not raw:
        return {"expires_at": None, "expired": False, "days_left": None, "reminder_due": False}
    now = now or datetime.now(timezone.utc)
    try:
        exp_at = datetime.fromisoformat(str(raw))
        if exp_at.tzinfo is None:
            exp_at = exp_at.replace(tzinfo=timezone.utc)
    except ValueError:
        return {"expires_at": str(raw), "expired": True, "days_left": 0, "reminder_due": False}
    left = (exp_at - now).total_seconds() / 86400
    import math
    return {"expires_at": exp_at.isoformat(), "expired": left <= 0, "days_left": max(0, math.ceil(left)),   # N108-5 — 2.9 d → "3 days", never rounded down
            "reminder_due": 0 < left <= POLICY_EXPIRY_REMINDER_DAYS}




async def demo_only_policy_active(db) -> bool:
    """A17-13 — True while the approved inventory expectation is a signed DEMO-only policy that has not expired."""
    exp = await db.platform_state.find_one({"_id": "inventory_expectation"}, projection={"demo_only": 1, "policy_expires_at": 1})
    return bool(exp and exp.get("demo_only") and not policy_expiry(exp)["expired"])

def migration_problems(payload: dict, *, current_policy_version: str, now: datetime | None = None) -> list:
    """Every reason a supplied policy migration is NOT acceptable (empty ⇒ valid)."""
    mig = payload.get("policy_migration") or {}
    if not mig.get("signature_hex"):
        return ["policy_migration.signature_hex missing"]
    now = now or datetime.now(timezone.utc)
    problems = []
    try:
        from release_signing import verify_hex
        if not verify_hex(migration_body(mig), str(mig["signature_hex"]), purpose="policy-migration"):
            problems.append("migration signature invalid")
    except Exception:  # noqa: BLE001
        problems.append("migration signature invalid")
    if mig.get("schema") != MIGRATION_SCHEMA:
        problems.append(f"migration schema must be {MIGRATION_SCHEMA}")
    inst = installation_id()
    if not inst or mig.get("installation_id") != inst:
        problems.append("migration installation_id does not match this installation")
    if mig.get("environment") != environment_label():
        problems.append(f"migration environment must be {environment_label()}")
    if mig.get("previous_policy_version") != current_policy_version:
        problems.append(f"migration previous_policy_version must be {current_policy_version}")
    if not mig.get("policy_version") or mig.get("policy_version") == current_policy_version:
        problems.append("migration policy_version must be a new version")
    import re as _re
    if not _re.fullmatch(POLICY_VERSION_RE, str(mig.get("policy_version") or "")):
        problems.append("migration policy_version must match " + POLICY_VERSION_RE)
    if not _re.fullmatch(PREVIOUS_POLICY_VERSION_RE, str(mig.get("previous_policy_version") or "")):
        problems.append("migration previous_policy_version must match " + PREVIOUS_POLICY_VERSION_RE)
    try:
        if (int(mig.get("accounts")), int(mig.get("enabled")), int(mig.get("bots"))) != \
                (int(payload["accounts"]), int(payload["enabled"]), int(payload["bots"])):
            problems.append("migration counts differ from the proposed expectation")
    except (TypeError, ValueError, KeyError):
        problems.append("migration counts invalid")
    if sorted(mig.get("account_ids") or []) != sorted(payload.get("account_ids") or []):
        problems.append("migration account_ids differ from the proposed expectation")
    try:
        issued, expires = datetime.fromisoformat(str(mig.get("issued_at"))), datetime.fromisoformat(str(mig.get("expires_at")))
        if issued.tzinfo is None or expires.tzinfo is None:
            raise ValueError("naive")
        if not (issued <= now < expires):
            problems.append("migration expired or not yet valid")
        if mig.get("demo_only") and (expires - issued) > timedelta(days=DEMO_POLICY_MAX_DAYS):
            problems.append(f"demo_only migration may be valid for at most {DEMO_POLICY_MAX_DAYS} days")   # A16-4
        if (expires - issued) > timedelta(days=POLICY_MAX_DAYS):
            problems.append(f"migration may be valid for at most {POLICY_MAX_DAYS} days")                   # SA7-P3
    except (TypeError, ValueError):
        problems.append("migration issued_at/expires_at invalid")
    if len(str(mig.get("nonce") or "")) < 16:
        problems.append("migration nonce missing")
    if not str(mig.get("issuer") or "").strip() or len(str(mig.get("reason") or "").strip()) < 10:
        problems.append("migration issuer/reason required")
    return problems


async def consume_migration_nonce(db, mig: dict) -> None:
    """Atomic single use — a replayed migration is refused even with a valid signature."""
    from fastapi import HTTPException
    try:
        await db.policy_migration_nonces.insert_one({"_id": str(mig.get("nonce")), "at": _now(),
                                                     "policy_version": mig.get("policy_version")})
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail={"code": "policy_migration_replayed"})


def validate_expectation(payload: dict, *, require_policy: bool,
                         current_policy_version: str = DEPLOYMENT_POLICY_VERSION) -> dict:
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
    mig = payload.get("policy_migration") or {}
    # N108-4 — without a signed policy the expectation is the built-in deployment policy: never inherit the
    # previous (possibly expired, demo-only) version label from the current expectation
    policy_version = current_policy_version if mig.get("signature_hex") else DEPLOYMENT_POLICY_VERSION
    demo_only = bool(mig.get("demo_only"))
    if demo_only and not mig.get("signature_hex"):
        errors.append("demo_only is only accepted inside a SIGNED policy migration")
    if require_policy and ((acc, en, bots) != (DEPLOYMENT_POLICY["accounts"], DEPLOYMENT_POLICY["enabled"], DEPLOYMENT_POLICY["bots"]) or demo_only):
        probs = migration_problems(payload, current_policy_version=current_policy_version)
        if probs:
            errors.append("production target must be 6/3/3 unless a valid signed policy migration is supplied: " + "; ".join(probs))
        else:
            policy_version = payload["policy_migration"]["policy_version"]
    if require_policy and not ids:
        errors.append("production expectation must name the approved account ids")
    if errors:
        raise HTTPException(status_code=400, detail={"code": "expectation_invalid", "errors": errors})
    return {"accounts": acc, "enabled": en, "bots": bots, "account_ids": ids, "policy_version": policy_version,
            "demo_only": demo_only}


async def current_policy_version(db) -> str:
    cur = await db.platform_state.find_one({"_id": "inventory_expectation"}, {"policy_version": 1}) or {}
    return str(cur.get("policy_version") or DEPLOYMENT_POLICY_VERSION)


async def propose_expectation(db, payload: dict, actor_email: str) -> dict:
    """Two-admin control (round 10 P1-01): one admin PROPOSES the desired state,
    a DIFFERENT admin (step-up verified) must approve before it takes effect."""
    v = validate_expectation(payload, require_policy=production_mode(),
                             current_policy_version=await current_policy_version(db))
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
    if approval_mode() == "four_eyes" and pending.get("proposed_by", "").lower() == approver_email.lower():
        raise HTTPException(status_code=403, detail={"code": "second_admin_required",
                                                     "message": "the proposing admin cannot approve their own expectation"})
    v = validate_expectation(pending, require_policy=production_mode(),
                             current_policy_version=await current_policy_version(db))   # re-validated at approval
    if pending.get("policy_migration") and v["policy_version"] != await current_policy_version(db):
        await consume_migration_nonce(db, pending["policy_migration"])                  # single use, atomic
    doc = {k: pending.get(k) for k in ("accounts", "enabled", "bots", "account_ids", "scope_user_id", "proposed_by", "proposed_at")}
    doc.update(_id="inventory_expectation", approved_by=approver_email, set_at=_now(), policy_version=v["policy_version"],
               demo_only=bool(v.get("demo_only")),            # SEC-002 (audit #5) — the signed DEMO-only guard must persist
               approval_mode=approval_mode(),
               # A16-4 — the signed policy's lifetime travels with the approval: an expired policy turns the
               # inventory close-only (projection) and raises an alert (alerting) until a new one is approved.
               policy_expires_at=((pending.get("policy_migration") or {}).get("expires_at")
                                  if pending.get("policy_migration") and v["policy_version"] != DEPLOYMENT_POLICY_VERSION else None),
               # A16-5 — a one-person operator may approve alone; the audit says so explicitly
               single_admin_approval=(approval_mode() == "single_admin"))
    await db.platform_state.replace_one({"_id": "inventory_expectation"}, doc, upsert=True)
    await db.platform_state.delete_one({"_id": "inventory_expectation_pending"})
    await append_chained(db, {"actor_email": approver_email, "action": "inventory_expectation_approved",
                              "target_kind": "platform", "target_id": "inventory_expectation",
                              "reason": f"proposed by {pending['proposed_by']}", "at": doc["set_at"],
                              "meta": {k: doc[k] for k in ("accounts", "enabled", "bots", "account_ids", "policy_version", "approval_mode",
                                                           "policy_expires_at", "single_admin_approval")}})
    from canonical_decision import bump_authority_version
    await bump_authority_version(db, "inventory_expectation")
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
