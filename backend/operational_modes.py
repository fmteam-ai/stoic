"""Autopilot #15 — explicit operational modes.

    observe          detect opportunities, never create trades
    shadow           record full simulated decisions from live data
    demo_autopilot   full system trades on demo accounts only
    supervised_live  real capital at half size (operator available)
    autonomous_live  approved strategies trade automatically (default)
    defensive        only manage/reduce existing exposure — no new entries
    panic            block new trades (position closing via the panic switch)

The gate runs AFTER the full analysis pipeline so observe/shadow modes still
produce studyable decisions — they are recorded in `mode_intercepts`.

Safety review: DEFAULT_MODE is OBSERVE — a missing or migrated configuration
can never default to live execution. Promotion toward live modes is explicit,
step-up-MFA'd, certification-gated and audited (see promotion_gate).
"""
import logging
from datetime import datetime, timezone

logger = logging.getLogger("operational-modes")

MODES = {
    "observe": {"rank": 2, "allow_new": False, "lot_scale": 0.0,
                "label": "OBSERVE",
                "detail": "opportunities detected and recorded — no orders created"},
    "shadow": {"rank": 3, "allow_new": False, "lot_scale": 0.0,
               "label": "SHADOW",
               "detail": "full simulated decisions recorded from live data"},
    "demo_autopilot": {"rank": 4, "allow_new": True, "lot_scale": 1.0,
                       "label": "DEMO AUTOPILOT", "demo_only": True,
                       "detail": "full system trades on demo accounts only"},
    "supervised_live": {"rank": 5, "allow_new": True, "lot_scale": 0.5,
                        "label": "SUPERVISED LIVE",
                        "detail": "real capital at half size while an operator is available"},
    "autonomous_live": {"rank": 6, "allow_new": True, "lot_scale": 1.0,
                        "label": "AUTONOMOUS LIVE",
                        "detail": "approved strategies trade automatically"},
    "defensive": {"rank": 1, "allow_new": False, "lot_scale": 0.0,
                  "label": "DEFENSIVE",
                  "detail": "only manages and reduces existing exposure"},
    "panic": {"rank": 0, "allow_new": False, "lot_scale": 0.0,
              "label": "PANIC",
              "detail": "new trades blocked — use the PANIC switch to flatten"},
}
DEFAULT_MODE = "observe"  # fail-safe: never default to live execution


def mode_gate(cfg: dict, account: dict) -> dict:
    """→ {mode, allow_new, lot_scale, reason}. Unknown mode fails closed
    to observe (never trade on a misconfigured mode)."""
    mode = (cfg or {}).get("operational_mode") or DEFAULT_MODE
    spec = MODES.get(mode)
    if spec is None:
        return {"mode": "observe", "allow_new": False, "lot_scale": 0.0,
                "reason": f"unknown mode '{mode}' — failing closed to observe"}
    if spec.get("demo_only") and str(
            (account or {}).get("mode") or "").lower() == "live":
        return {"mode": mode, "allow_new": False, "lot_scale": 0.0,
                "reason": "demo autopilot — live accounts do not execute"}
    return {"mode": mode, "allow_new": spec["allow_new"],
            "lot_scale": spec["lot_scale"], "reason": spec["detail"]}


async def record_intercept(db, user_id: str, account: dict, signal: dict,
                           lot: float, mg: dict) -> None:
    """Observe/shadow/defensive decisions are valuable — keep them."""
    await db.mode_intercepts.insert_one({
        "user_id": user_id,
        "account_id": str((account or {}).get("_id") or ""),
        "mode": mg["mode"],
        "symbol": signal.get("symbol"),
        "action": signal.get("action"),
        "confidence": signal.get("confidence"),
        "entry_price": signal.get("entry_price"),
        "stop_loss": signal.get("stop_loss"),
        "take_profit": signal.get("take_profit"),
        "lot_size": lot,
        "scope": signal.get("scope"),
        "consensus": (signal.get("consensus") or {}).get("score"),
        "monte_carlo_ev_r": (signal.get("monte_carlo") or {}).get(
            "ev_r_net", (signal.get("monte_carlo") or {}).get("ev_r")),
        "at": datetime.now(timezone.utc),
    })


async def migrate_default_modes(db) -> dict:
    """Safety migration (revised iter-103): legacy configs get an EXPLICIT
    mode, but NO config silently obtains full autonomous authority. Active
    legacy configs are stamped supervised_live (half size, operator in the
    loop); autonomous_live requires the explicit certification-gated,
    step-up-MFA'd promotion. Idempotent — only touches configs missing the
    field."""
    now = datetime.now(timezone.utc)
    r_active = await db.bot_configs.update_many(
        {"operational_mode": {"$exists": False}, "active": True},
        {"$set": {"operational_mode": "supervised_live",
                  "mode_migrated_at": now.isoformat(),
                  "mode_migration_policy":
                      "legacy_active_to_supervised_live"}})
    r_inactive = await db.bot_configs.update_many(
        {"operational_mode": {"$exists": False}},
        {"$set": {"operational_mode": "observe"}})
    if r_active.modified_count or r_inactive.modified_count:
        await db.audit_log.insert_one({
            "user_id": "system", "action": "operational_mode_migration",
            "detail": {"legacy_active_to_supervised_live":
                       r_active.modified_count,
                       "defaulted_inactive_to_observe":
                       r_inactive.modified_count,
                       "reason": "no migrated config silently obtains "
                                 "autonomous authority — explicit promotion "
                                 "required for autonomous_live"},
            "step_up_verified": False, "at": now})
        logger.warning("operational-mode migration: %d active→supervised_live"
                       ", %d inactive→observe", r_active.modified_count,
                       r_inactive.modified_count)
    return {"active_grandfathered": r_active.modified_count,
            "inactive_defaulted": r_inactive.modified_count}


async def remigrate_autonomous_to_supervised(db) -> dict:
    """One-time safety re-migration (iter-103): configs that obtained
    autonomous_live from the ORIGINAL grandfather migration (i.e. without a
    recorded explicit mode_promotion) are demoted to supervised_live. They
    keep trading at half size; full autonomy needs explicit re-promotion
    through the certification gate. Idempotent via platform_state flag."""
    flag = await db.platform_state.find_one(
        {"_id": "mode_safety_remigration"})
    if flag and flag.get("done"):
        return {"demoted": 0, "already_done": True}
    now = datetime.now(timezone.utc)
    demoted = 0
    async for cfg in db.bot_configs.find(
            {"operational_mode": "autonomous_live"}):
        if cfg.get("mode_explicitly_promoted"):
            continue
        promoted = await db.governed_changes.find_one({
            "user_id": cfg.get("user_id"),
            "field": "operational_mode",
            "new_value": "autonomous_live",
            "source": "mode_promotion", "status": "approved"})
        if promoted:
            continue
        await db.bot_configs.update_one(
            {"_id": cfg["_id"]},
            {"$set": {"operational_mode": "supervised_live",
                      "mode_migrated_at": now.isoformat(),
                      "mode_migration_policy":
                          "remigration_autonomous_to_supervised"}})
        demoted += 1
    await db.platform_state.update_one(
        {"_id": "mode_safety_remigration"},
        {"$set": {"done": True, "demoted": demoted, "at": now}},
        upsert=True)
    if demoted:
        await db.audit_log.insert_one({
            "user_id": "system",
            "action": "operational_mode_safety_remigration",
            "detail": {"demoted_to_supervised_live": demoted,
                       "reason": "grandfathered autonomous_live withdrawn — "
                                 "explicit certification-gated promotion "
                                 "required for full autonomy"},
            "step_up_verified": False, "at": now})
        try:
            from alerting import raise_alert
            await raise_alert(
                db, "mode_safety_remigration", "warning",
                f"{demoted} bot config(s) demoted from autonomous_live to "
                f"supervised_live (safety re-migration). Re-promote "
                f"explicitly via the certification gate when ready.",
                dedup_key="mode_safety_remigration")
        except Exception as e:  # noqa: BLE001
            logger.warning("remigration alert failed: %s", e)
        logger.warning("mode safety re-migration: %d autonomous_live → "
                       "supervised_live", demoted)
    return {"demoted": demoted, "already_done": False}


# ---------------------------------------------------------------------------
# Promotion gate — explicit, audited, certification-tied (user choice 2a)
# ---------------------------------------------------------------------------
LIVE_MODES = {"supervised_live", "autonomous_live"}


def evaluate_promotion(target_mode: str, certs: list[dict]) -> dict:
    """Pure certification rules. `certs` = [{account, tier}] for LIVE
    accounts only. DEGRADED blocks any live mode; PROVISIONAL allowed up to
    supervised_live (with warning); autonomous_live needs CERTIFIED or
    ACCEPTABLE on every live account."""
    blockers, warnings = [], []
    if target_mode in LIVE_MODES:
        for c in certs:
            tier = (c.get("tier") or "PROVISIONAL").upper()
            if tier == "DEGRADED":
                blockers.append(
                    f"{c.get('account')}: broker certification DEGRADED — "
                    f"blocked from live modes ({c.get('detail') or 'poor measured execution'})")
            elif tier == "PROVISIONAL":
                if target_mode == "autonomous_live":
                    blockers.append(
                        f"{c.get('account')}: certification PROVISIONAL — "
                        f"autonomous_live requires CERTIFIED or ACCEPTABLE "
                        f"(≥10 measured fills). Promote to supervised_live first.")
                else:
                    warnings.append(
                        f"{c.get('account')}: certification PROVISIONAL — "
                        f"allowed at supervised_live (half size) while fills accrue")
    return {"allowed": not blockers, "blockers": blockers,
            "warnings": warnings}


async def promotion_gate(db, user_id: str, account_id: str | None,
                         target_mode: str) -> dict:
    """Full promotion evidence: broker certification per live account,
    backend release, EA versions/heartbeats, deployment stage, last broker
    validation. Certification rules can BLOCK; the rest is recorded evidence."""
    from broker_intel import score_account
    from differentiation import certify
    q = {"user_id": user_id, "status": {"$ne": "deleted"},
         "dormant": {"$ne": True}, "harness": {"$ne": True}}
    if account_id:
        from bson import ObjectId
        q["_id"] = ObjectId(account_id)
    certs, ea = [], []
    async for acc in db.accounts.find(q):
        if str(acc.get("mode") or "").lower() == "live" or account_id:
            res = await score_account(db, acc)
            c = certify(res["score"], res["provisional"],
                        res["fills_measured"])
            certs.append({"account": (res.get("account_number")
                                      or res["label"]),
                          "account_label": res["label"],
                          "account_id": res["account_id"],
                          "account_number": res.get("account_number"),
                          "broker_server": res.get("broker_server"),
                          "tier": c["tier"],
                          "detail": c.get("detail"),
                          "fills_measured": res["fills_measured"]})
        ea.append({"account": (acc.get("broker_account_id_reported")
                               or acc.get("account_number")
                               or acc.get("label")),
                   "account_label": acc.get("label"),
                   "account_id": str(acc["_id"]),
                   "ea_version": acc.get("ea_version"),
                   "last_heartbeat": str(acc.get("last_heartbeat") or "")})
    verdict = evaluate_promotion(target_mode, certs)
    from versioning import version_stamp
    stage = await db.platform_state.find_one({"_id": "deployment_stage"}) or {}
    last_validation = await db.validation_runs.find_one(
        {}, sort=[("at", -1)],
        projection={"run_id": 1, "mode": 1, "passed": 1, "failed": 1, "at": 1})
    verdict["evidence"] = {
        "target_mode": target_mode,
        "certifications": certs,
        "backend_release": version_stamp(),
        "ea_accounts": ea,
        "deployment_stage": stage.get("stage"),
        "last_broker_validation": {
            "campaign": (last_validation or {}).get("run_id"),
            "mode": (last_validation or {}).get("mode"),
            "passed": (last_validation or {}).get("passed"),
            "failed": (last_validation or {}).get("failed"),
            "at": str((last_validation or {}).get("at") or "")}
        if last_validation else None,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }
    # Phase 1.4 — shadow health pauses live promotions automatically
    if target_mode in LIVE_MODES:
        try:
            from shadow_health import THRESHOLD, health_score
            hs = await health_score(db, user_id)
            verdict["evidence"]["shadow_health"] = hs
            if hs.get("promotions_paused"):
                verdict["blockers"].append(
                    f"shadow health {hs['overall']} < {THRESHOLD} — "
                    f"promotions paused until system health recovers")
                verdict["allowed"] = False
        except Exception as e:  # noqa: BLE001 — health probe must not crash the gate
            logger.warning("shadow health probe failed: %s", e)
        # Correction #6 — after an automatic demotion, recovery to a live
        # mode requires a stable green period. Probe errors fail CLOSED
        # (this path only ever RAISES authority).
        try:
            from auto_demotion import recovery_status
            rec = await recovery_status(db, user_id)
            verdict["evidence"]["auto_demotion_recovery"] = rec
            if rec["applicable"] and not rec["eligible"]:
                verdict["blockers"].append(
                    f"auto-demotion recovery: {rec['reason']}")
                verdict["allowed"] = False
        except Exception as e:  # noqa: BLE001
            logger.warning("recovery probe failed — blocking promotion "
                           "(fail closed): %s", e)
            verdict["blockers"].append(
                "auto-demotion recovery status unavailable — promotion "
                "blocked (fail closed)")
            verdict["allowed"] = False
    # Phase 2.4 — autonomous authority requires statistical evidence (CIs)
    if target_mode == "autonomous_live":
        try:
            from statistical_validation import promotion_evidence
            ev = await promotion_evidence(db, user_id)
            verdict["evidence"]["statistical_validation"] = ev
            if not ev["sufficient"]:
                verdict["blockers"].extend(ev["blockers"])
                verdict["allowed"] = False
        except Exception as e:  # noqa: BLE001
            logger.warning("statistical validation probe failed: %s", e)
    return verdict
