"""U9 (Phase 1) — automatic DEMO attestation from broker truth + a signed EA.

An admin used to attest DEMO by hand (password + 2FA + reason). Everything that attestation checked is
machine-verifiable, so it is granted automatically — and ONLY — when all of this holds on a fresh heartbeat:
  · the identity chain is authoritative (token + installation + login + server),
  · the broker itself reports ACCOUNT_TRADE_MODE_DEMO (EA 1.60+),
  · the EA binary hash reported on this heartbeat is one of the v2-signed release hashes
    (ea_capabilities.accepted_ea_sha256s) — an unknown binary attests nothing,
  · the declared classification is DEMO and demo_proof() passes (same checks the admin path used).
Voided (unset + audited) the moment the broker reports real/contest money. Any unevaluable check → no
attestation (fail closed). Manual admin attestation / override is untouched; an admin attestation is
never overwritten by the automatic one."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from audit_chain import append_chained
from broker_env import (attestation_identity, broker_environment, broker_reports_demo, broker_reports_real,
                        demo_proof)

logger = logging.getLogger(__name__)
AUTO_APPROVER = "system:auto-demo"
AUTO_VERIFIER = "ea_heartbeat_auto"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def evaluate(acc: dict, *, accepted_hashes: list, now=None) -> dict:
    """Pure decision. Returns {"grant": bool, "void": bool, "checks": {...}, "proof": demo_proof|None}."""
    att = acc.get("environment_attestation") or {}
    if acc.get("mode") == "paper":
        return {"grant": False, "void": False, "checks": {"paper": True}, "proof": None}
    if broker_reports_real(acc):
        # broker truth wins: void whatever is there (manual or automatic)
        return {"grant": False, "void": bool(att.get("approved_by")), "checks": {"broker_reports_real": True}, "proof": None}
    ident = acc.get("ea_identity") or {}
    # N99-2 guardrail kept: only the hash the INSTALLER measured on the deployed EX5 counts (a self-compiled EA
    # echoes whatever it likes in the heartbeat) — and it must be one of the v2-signed release hashes.
    measured = str(acc.get("ea_binary_sha256") or "").lower()
    proof = demo_proof(acc, now=now)
    checks = {
        "identity_authoritative": bool(ident.get("authoritative")) and bool(ident.get("installation_id")),
        "broker_reports_demo": broker_reports_demo(acc),
        "declared_demo": broker_environment(acc) == "DEMO",
        "ea_hash_signed": str(acc.get("ea_binary_sha256_method") or "") == "installer_attested" and bool(measured)
                          and measured in {str(h).lower() for h in (accepted_hashes or [])},
        "demo_proof_ok": bool(proof["ok"]),
    }
    if att.get("approved_by") and att.get("approved_by") != AUTO_APPROVER:
        return {"grant": False, "void": False, "checks": {**checks, "manual_attestation_present": True}, "proof": proof}
    already = (att.get("approved_by") == AUTO_APPROVER and att.get("identity_hash") == attestation_identity(acc))
    if att.get("approved_by") == AUTO_APPROVER:
        # re-check on EVERY heartbeat (main127 open item): identity moved, the binary is no longer a signed
        # release, the broker no longer says demo, or the proof fails → the automatic grant is voided
        stale = att.get("identity_hash") != attestation_identity(acc)
        if stale or not all(checks.values()):
            return {"grant": False, "void": True, "checks": {**checks, "recheck_failed": True, "identity_moved": stale}, "proof": proof}
    # prod v1.60.23 (bug 2a): the broker itself reports DEMO on a verified terminal running the signed, installer-
    # attested EA, but the owner's record never DECLARED demo (type left at "microcent" → classified LIVE). Broker
    # truth outranks a missing declaration: declare it DEMO automatically (audited), then the grant follows.
    others = {k: v for k, v in checks.items() if k != "declared_demo"}
    declare = (not checks["declared_demo"]) and all(others.values()) and not acc.get("broker_environment")
    return {"grant": all(checks.values()) and not already, "void": False, "declare_demo": declare, "checks": checks, "proof": proof}


async def apply(db, acc: dict, *, accepted_hashes: list | None = None) -> str | None:
    """Heartbeat hook. Returns 'granted' | 'voided' | None. Never raises into the heartbeat."""
    if accepted_hashes is None:
        from ea_capabilities import accepted_ea_sha256s
        accepted_hashes = accepted_ea_sha256s()
    d = evaluate(acc, accepted_hashes=accepted_hashes)
    now = _now()
    if d.get("declare_demo"):
        await db.accounts.update_one({"_id": acc["_id"]}, {"$set": {"broker_environment": "DEMO", "declared_environment": "demo",
                                                                    "environment_declared_auto": {"at": now, "source": "broker_trade_mode_demo", "ea_binary_sha256": acc.get("ea_binary_sha256")}}})
        await append_chained(db, {"actor_email": "system", "action": "account_environment_declared_demo_auto",
                                  "target_kind": "account", "target_id": str(acc["_id"]),
                                  "reason": "broker reports ACCOUNT_TRADE_MODE_DEMO on a verified terminal running the signed, installer-attested EA — declared DEMO automatically (owner left the type at its default)",
                                  "at": now, "meta": {"previous_account_type": acc.get("account_type"), "previous_broker_environment": acc.get("broker_environment"),
                                                      "checks": d["checks"]}})
        acc = {**acc, "broker_environment": "DEMO", "declared_environment": "demo"}
        d = evaluate(acc, accepted_hashes=accepted_hashes)
    if d["void"]:
        att = acc.get("environment_attestation") or {}
        recheck = bool(d["checks"].get("recheck_failed"))
        await db.accounts.update_one({"_id": acc["_id"]}, {"$unset": {"environment_attestation": ""}})
        await append_chained(db, {"actor_email": "system", "action": "account_environment_attestation_voided",
                                  "target_kind": "account", "target_id": str(acc["_id"]),
                                  "reason": ("automatic DEMO attestation re-check failed on this heartbeat (identity/binary/broker/proof) — voided"
                                             if recheck else
                                             "broker reports real/contest money — DEMO attestation voided (U9, broker truth wins)"),
                                  "at": now, "meta": {"previous_approved_by": att.get("approved_by"),
                                                      "reported_trade_mode": acc.get("account_trade_mode"),
                                                      "checks": d["checks"]}})
        acc.pop("environment_attestation", None)
        return "voided"
    if not d["grant"]:
        return None
    proof = d["proof"]
    att = {"environment": "DEMO", "approved_by": AUTO_APPROVER, "at": now,
           "reason": "automatic: broker reports ACCOUNT_TRADE_MODE_DEMO on a verified terminal running a signed EA",
           "identity_hash": attestation_identity(acc),
           "proof": {"proof_id": proof["proof_id"], "verifier": AUTO_VERIFIER, "checks": {**proof["checks"], **d["checks"]},
                     "heartbeat_age_s": proof["heartbeat_age_s"], "reported_server": proof["reported_server"],
                     "ea_binary_sha256": acc.get("ea_binary_sha256"), "at": now}}
    await db.accounts.update_one({"_id": acc["_id"]}, {"$set": {"environment_attestation": att}})
    await append_chained(db, {"actor_email": "system", "action": "account_environment_attest",
                              "target_kind": "account", "target_id": str(acc["_id"]),
                              "reason": att["reason"], "at": now,
                              "meta": {"environment": "DEMO", "verifier": AUTO_VERIFIER, "proof_id": proof["proof_id"],
                                       "checks": att["proof"]["checks"], "ea_binary_sha256": acc.get("ea_binary_sha256"),
                                       "identity_hash": att["identity_hash"]}})
    logger.info("auto DEMO attestation granted account=%s proof=%s", acc["_id"], proof["proof_id"])
    return "granted"
