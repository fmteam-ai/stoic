"""A13 P1-01 — signed operational acceptance bundle.

A bundle is the proof that the LIVE setup actually running is the one approved:
account and bot ids, environments, enabled states, EA sessions, reconciliation
times, positions, executions, authority blockers and the broker-statement
comparison, bound to the deployed release identity (build SHA + image digest)
and HMAC-signed with LEDGER_ANCHOR_KEY. Live (real-money) authority unlocks only
while a PASSING bundle for the CURRENT release covers the account.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

BUNDLE_TTL_DAYS = 7
EA_SESSION_FRESH_SEC = 300
RECON_FRESH_SEC = 24 * 3600
STATEMENT_TOLERANCE = 0.01


def _now() -> datetime:
    return datetime.now(timezone.utc)


def release_identity() -> dict:
    from modules.pamm.strategy_guard import GIT_COMMIT
    from ea_capabilities import accepted_ea_sha256s, shipped_ea_version
    acc = accepted_ea_sha256s()
    return {"build_sha": GIT_COMMIT, "image_digest": os.environ.get("STOIC_IMAGE_DIGEST") or None,
            "ea_version": shipped_ea_version(), "ea_sha256": acc[0] if acc else None,
            "environment": os.environ.get("APP_ENV") or "dev"}


SIGNED_FIELDS = ("bundle_id", "payload", "verdict", "failures", "account_ids", "build_sha", "image_digest",
                 "created_by", "created_at", "expires_at")


def _canonical(bundle: dict) -> bytes:
    """N97-3 — the signature covers EVERY field bundle_covers() decides on, not just the payload."""
    body = {k: bundle.get(k) for k in SIGNED_FIELDS}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()


def _sign(bundle: dict) -> tuple[str, str]:
    body = _canonical(bundle)
    digest = hashlib.sha256(body).hexdigest()
    key = (os.environ.get("LEDGER_ANCHOR_KEY") or "").encode()
    if not key:
        raise RuntimeError("LEDGER_ANCHOR_KEY unset — acceptance bundles must be signed")
    return digest, hmac.new(key, body, hashlib.sha256).hexdigest()


def verify_signature(bundle: dict) -> bool:
    try:
        digest, sig = _sign(bundle)
    except Exception:  # noqa: BLE001
        return False
    return hmac.compare_digest(sig, str(bundle.get("signature") or "")) and digest == bundle.get("digest")


def _age_s(iso: str | None, now: datetime) -> float | None:
    if not iso:
        return None
    try:
        return (now - datetime.fromisoformat(str(iso).replace("Z", "+00:00"))).total_seconds()
    except ValueError:
        return None


async def _statement_comparison(db, acc: dict) -> dict:
    """Broker statement vs platform: latest reconciled ledger row for the account."""
    row = await db.reconciliation_ledger.find_one({"account_id": str(acc["_id"])}, sort=[("period_to", -1)])
    if not row:
        return {"status": "MISSING", "discrepancy": None}
    disc = row.get("discrepancy")
    if disc is None:
        disc = abs(float(row.get("broker_net") or 0) - float(row.get("platform_net") or 0))
    return {"status": "RECONCILED" if float(disc) <= STATEMENT_TOLERANCE else "GAP",
            "discrepancy": round(float(disc), 4), "period_to": row.get("period_to"), "ledger_seq": row.get("seq")}


async def account_evidence(db, acc: dict, now: datetime) -> dict:
    from broker_env import attested_environment
    from trading_authority import compute_authority, level_severity
    aid = str(acc["_id"])
    ident = acc.get("ea_identity") or {}
    hb_age = _age_s(acc.get("last_heartbeat"), now)
    recon_age = _age_s(acc.get("last_full_sync_at") or acc.get("last_reconciled_at"), now)
    bots = [{"bot_config_id": str(c["_id"]), "active": bool(c.get("active")), "preset": c.get("active_preset"),
             "trade_of_day_cap": c.get("trade_of_day_cap"), "max_concurrent_trades": c.get("max_concurrent_trades")}
            async for c in db.bot_configs.find({"account_id": aid}, {"active": 1, "active_preset": 1,
                                                                      "trade_of_day_cap": 1, "max_concurrent_trades": 1})]
    auth = await compute_authority(db, acc)
    unknown = await db.execution_intents.count_documents({"account_id": aid, "status": "unknown"})
    since = (now - timedelta(hours=24)).isoformat()
    return {
        "account_id": aid, "label": acc.get("display_name") or acc.get("label") or acc.get("broker"),
        # N97-11 — environment by ADMIN ATTESTATION (identity-bound), never by heartbeat freshness
        "mode": acc.get("mode"), "environment": attested_environment(acc),
        "trading_enabled": acc.get("trading_enabled") is True,
        "broker_server": ident.get("broker_server") or acc.get("broker_server"),
        "ea_session": {"installation_id": ident.get("installation_id") or acc.get("installation_id"),
                       "ea_version": acc.get("ea_version"), "heartbeat_age_s": hb_age,
                       "fresh": hb_age is not None and hb_age <= EA_SESSION_FRESH_SEC},
        "reconciliation": {"last_at": acc.get("last_full_sync_at"), "age_s": recon_age,
                           "seq": acc.get("reconciliation_seq"),
                           "fresh": recon_age is not None and recon_age <= RECON_FRESH_SEC},
        "positions": {"open": await db.trades.count_documents({"account_id": aid, "status": "open"}),
                      "pending": await db.trades.count_documents({"account_id": aid, "status": "pending"})},
        "executions_24h": await db.trades.count_documents({"account_id": aid, "opened_at": {"$gte": since}}),
        "unknown_intents": unknown,
        "bots": bots,
        # the acceptance domain itself is excluded — the bundle is what unlocks it
        "authority": {"level": max((v["level"] for k, v in auth["domains"].items() if k != "acceptance"),
                                   key=level_severity),
                      "blockers": [f"{k}: {v['reason']}" for k, v in auth["domains"].items()
                                   if k != "acceptance" and v["level"] != "FULL"]},
        "statement": await _statement_comparison(db, acc),
    }


def account_failures(ev: dict) -> list[str]:
    out = []
    if not ev["trading_enabled"]:
        return out                                     # disabled accounts carry no live exposure
    if not ev["ea_session"]["fresh"]:
        out.append("EA session not fresh (≤5 min heartbeat required)")
    if not ev["reconciliation"]["fresh"]:
        out.append("no broker reconciliation in 24 h")
    if ev["authority"]["level"] != "FULL":
        out.append("trading authority not FULL: " + "; ".join(ev["authority"]["blockers"][:3]))
    if ev["unknown_intents"]:
        out.append(f"{ev['unknown_intents']} UNKNOWN execution intent(s)")
    if ev["environment"] == "LIVE" and ev["statement"]["status"] != "RECONCILED":
        out.append(f"broker statement {ev['statement']['status']}"
                   + (f" (Δ {ev['statement']['discrepancy']})" if ev["statement"]["discrepancy"] is not None else ""))
    return out


async def build_bundle(db, *, actor: str) -> dict:
    now = _now()
    rel = release_identity()
    accounts = []
    base_q = {"mode": {"$ne": "paper"}, "status": {"$ne": "deleted"}}
    async for acc in db.accounts.find({**base_q, "trading_enabled": True}).limit(50):
        ev = await account_evidence(db, acc, now)
        ev["failures"] = account_failures(ev)
        accounts.append(ev)
    payload = {"release": rel, "generated_at": now.isoformat(), "actor": actor,
               "inventory": {"accounts": await db.accounts.count_documents(base_q),
                             "enabled": len(accounts),
                             "bots_active": sum(1 for a in accounts for b in a["bots"] if b["active"])},
               "accounts": accounts}
    failures = [f"{a['label']}: {f}" for a in accounts for f in a["failures"]]
    if not any(a["trading_enabled"] for a in accounts):
        failures.append("no enabled live account to accept")
    doc = {"bundle_id": uuid.uuid4().hex, "payload": payload,
           "algo": "hmac-sha256(LEDGER_ANCHOR_KEY) over bundle_id+payload+verdict+failures+account_ids+release+times",
           "verdict": "PASS" if not failures else "FAIL",
           "failures": failures, "build_sha": rel["build_sha"], "image_digest": rel["image_digest"],
           "created_by": actor, "created_at": now.isoformat(),
           "expires_at": (now + timedelta(days=BUNDLE_TTL_DAYS)).isoformat(),
           "account_ids": [a["account_id"] for a in accounts if a["trading_enabled"]]}
    doc["digest"], doc["signature"] = _sign(doc)
    await db.acceptance_bundles.insert_one(dict(doc))
    doc.pop("_id", None)
    return doc


def bundle_covers(bundle: dict | None, account_id: str, rel: dict, now: datetime | None = None) -> tuple[bool, str]:
    now = now or _now()
    if not bundle:
        return False, "no acceptance bundle"
    if bundle.get("verdict") != "PASS":
        return False, "latest acceptance bundle FAILED"
    if not verify_signature(bundle):
        return False, "acceptance bundle signature invalid"
    if str(bundle.get("expires_at") or "") < now.isoformat():
        return False, "acceptance bundle expired"
    if rel.get("image_digest") and bundle.get("image_digest") != rel["image_digest"]:
        return False, "acceptance bundle is for a different image digest"
    if bundle.get("build_sha") != rel.get("build_sha"):
        return False, "acceptance bundle is for a different build"
    if account_id not in (bundle.get("account_ids") or []):
        return False, "account not covered by the acceptance bundle"
    return True, "accepted"


async def latest_bundle(db) -> dict | None:
    return await db.acceptance_bundles.find_one({}, {"_id": 0}, sort=[("created_at", -1)])


def required() -> bool:
    from app_env import is_production
    return is_production() or os.environ.get("ACCEPTANCE_BUNDLE_REQUIRED", "false").lower() == "true"


async def current_status(db) -> dict:
    """N97-11 — coverage is reported for EVERY enabled live account, not just the first."""
    from broker_env import attested_environment
    rel = release_identity()
    b = await latest_bundle(db)
    public_b = {k: v for k, v in (b or {}).items() if k != "signature"} if b else None   # audit P3
    accounts = []
    async for acc in db.accounts.find({"mode": {"$ne": "paper"}, "trading_enabled": True,
                                       "status": {"$ne": "deleted"}}, {"label": 1, "display_name": 1,
                                                                        "environment_attestation": 1, "broker": 1,
                                                                        "server": 1, "broker_server": 1, "account_number": 1,
                                                                        "account_type": 1, "ea_identity": 1,
                                                                        "broker_account_id_reported": 1, "creds_version": 1,
                                                                        "broker_environment": 1, "mode": 1}).limit(50):
        env = attested_environment(acc)
        ok, why = bundle_covers(b, str(acc["_id"]), rel)
        accounts.append({"account_id": str(acc["_id"]), "label": acc.get("display_name") or acc.get("label") or acc.get("broker"),
                         "environment": env, "covered": ok, "reason": why,
                         "gate_applies": env == "LIVE"})
    live = [a for a in accounts if a["gate_applies"]]
    return {"required": required(), "release": rel, "latest": public_b,
            "valid_for_release": bool(live) and all(a["covered"] for a in live),
            "reason": ("all live accounts covered" if live and all(a["covered"] for a in live)
                       else (next((a["reason"] for a in live if not a["covered"]), None) or "no live account enabled")),
            "accounts": accounts}
