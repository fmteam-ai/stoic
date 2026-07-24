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
    """One-time grandfather migration (user choice 1a): configs that predate
    the operational-mode system get an EXPLICIT mode so flipping the default
    to observe cannot silently stop a running live bot. Active configs are
    stamped autonomous_live (status-quo, audited); inactive ones observe.
    Idempotent — only touches configs missing the field."""
    now = datetime.now(timezone.utc)
    r_active = await db.bot_configs.update_many(
        {"operational_mode": {"$exists": False}, "active": True},
        {"$set": {"operational_mode": "autonomous_live"}})
    r_inactive = await db.bot_configs.update_many(
        {"operational_mode": {"$exists": False}},
        {"$set": {"operational_mode": "observe"}})
    if r_active.modified_count or r_inactive.modified_count:
        await db.audit_log.insert_one({
            "user_id": "system", "action": "operational_mode_migration",
            "detail": {"grandfathered_active_to_autonomous_live":
                       r_active.modified_count,
                       "defaulted_inactive_to_observe":
                       r_inactive.modified_count,
                       "reason": "DEFAULT_MODE changed to observe — explicit "
                                 "stamps preserve running-bot behavior"},
            "step_up_verified": False, "at": now})
        logger.warning("operational-mode migration: %d active→autonomous_live"
                       ", %d inactive→observe", r_active.modified_count,
                       r_inactive.modified_count)
    return {"active_grandfathered": r_active.modified_count,
            "inactive_defaulted": r_inactive.modified_count}


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
            certs.append({"account": res["label"], "tier": c["tier"],
                          "detail": c.get("detail"),
                          "fills_measured": res["fills_measured"]})
        ea.append({"account": acc.get("label"),
                   "ea_version": acc.get("ea_version"),
                   "last_heartbeat": str(acc.get("last_heartbeat") or "")})
    verdict = evaluate_promotion(target_mode, certs)
    from versioning import version_stamp
    stage = await db.platform_state.find_one({"_id": "deployment_stage"}) or {}
    last_validation = await db.validation_runs.find_one(
        {}, sort=[("at", -1)], projection={"campaign": 1, "passed": 1, "at": 1})
    verdict["evidence"] = {
        "target_mode": target_mode,
        "certifications": certs,
        "backend_release": version_stamp(),
        "ea_accounts": ea,
        "deployment_stage": stage.get("stage"),
        "last_broker_validation": {
            "campaign": (last_validation or {}).get("campaign"),
            "passed": (last_validation or {}).get("passed"),
            "at": str((last_validation or {}).get("at") or "")}
        if last_validation else None,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }
    return verdict
