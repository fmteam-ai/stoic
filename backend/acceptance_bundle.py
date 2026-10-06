"""A13 P1-01 — signed operational acceptance bundle.

A bundle is the proof that the LIVE setup actually running is the one approved:
account and bot ids, environments, enabled states, EA sessions, reconciliation
times, positions, executions, authority blockers and the broker-statement
comparison and the approved inventory expectation (A14-9), bound to the deployed
release identity (build SHA + image digest) and a configuration fingerprint
(A14-11), Ed25519-signed through the release signer (A14-10). Live (real-money)
authority unlocks only while a PASSING, unexpired bundle for the CURRENT release
and UNCHANGED configuration covers the account.
"""
from __future__ import annotations

import hashlib
import json

from bson import ObjectId
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
                 "created_by", "created_at", "expires_at", "config_fingerprint", "schema_version", "key_id", "algo")
SCHEMA_VERSION = 2          # A14-10 — v2: Ed25519 via the release signer, fingerprint-bound (A14-11), inventory inside (A14-9)


def _canonical(bundle: dict) -> bytes:
    """N97-3 — the signature covers EVERY field bundle_covers() decides on, not just the payload."""
    body = {k: bundle.get(k) for k in SIGNED_FIELDS}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()


def _sign(bundle: dict) -> tuple[str, str]:
    """A14-10 — asymmetric: Ed25519 through release_signing (external signer in production; the
    trading server holds only RELEASE_PUBLIC_KEY_B64 and can never mint a valid signature itself)."""
    from release_signing import sign_hex, key_id
    body = _canonical(bundle)
    digest = hashlib.sha256(body).hexdigest()
    return digest, sign_hex(body, purpose="acceptance-bundle")


def revoked_key_ids() -> set:
    from release_signing import revoked_key_ids as _r
    return _r()


def verify_signature(bundle: dict) -> bool:
    """Ed25519 over the canonical body with the PINNED public key; the bundle's key id must be the
    current one and not revoked. Legacy shared-secret (schema v1) bundles are refused."""
    from release_signing import verify_hex, key_id
    if int(bundle.get("schema_version") or 1) < SCHEMA_VERSION:
        return False
    kid = str(bundle.get("key_id") or "")
    if not kid or kid in revoked_key_ids() or kid != key_id():
        return False
    try:
        body = _canonical(bundle)
        if hashlib.sha256(body).hexdigest() != bundle.get("digest"):
            return False
        return verify_hex(body, str(bundle.get("signature") or ""), purpose="acceptance-bundle")
    except Exception:  # noqa: BLE001
        return False


_FP_ACCOUNT_FIELDS = ("ea_binary_sha256", "ea_binary_sha256_method", "broker_server", "server", "account_number",
                      "creds_version", "trading_enabled", "mode", "account_type", "environment_attestation")
_FP_BOT_FIELDS = ("active", "symbols", "strategy", "strategy_mode", "risk_level", "risk_percent", "lot_size",
                  "max_concurrent", "max_concurrent_trades", "trade_of_day_cap", "max_trades_per_day",
                  "daily_drawdown_limit", "weekly_drawdown_limit", "max_daily_loss", "max_drawdown_pct",
                  "max_lot", "max_exposure", "sl_pips", "tp_pips", "timeframe", "account_id", "user_id")


def _fp_view(doc: dict, fields: tuple) -> dict:
    """N100-12 — ONLY configuration: an explicit field list; never `_`-prefixed runtime fields
    (_last_pulse, _tick_lock_until…) nor *_at / verified_identity stamps every heartbeat rewrites."""
    return {k: doc.get(k) for k in fields if k in doc}


async def config_fingerprint(db, account_ids: list, rel: dict) -> str:
    """A14-11 — everything the acceptance vouches for: accounts, bots (risk config), credentials,
    EA hashes, broker servers, inventory approval, release. Any CONFIG change ⇒ coverage void at once."""
    parts = {"release": f"{rel.get('build_sha')}|{rel.get('image_digest')}"}
    for aid in sorted(account_ids):
        a = await db.accounts.find_one({"_id": ObjectId(aid)}) or {}
        bots = []
        async for b in db.bot_configs.find({"account_id": aid}):
            bots.append(json.dumps(_fp_view(b, _FP_BOT_FIELDS), sort_keys=True, default=str))
        parts[aid] = json.dumps(_fp_view(a, _FP_ACCOUNT_FIELDS), sort_keys=True, default=str) + "|" + "|".join(sorted(bots))
    exp = await db.platform_state.find_one({"_id": "inventory_expectation"}) or {}
    parts["inventory_expectation"] = json.dumps({k: v for k, v in exp.items() if k not in ("_id",) and not k.endswith("_at")},
                                                sort_keys=True, default=str)
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def inventory_failures(proj: dict, exp: dict) -> list[str]:
    """A14-9 — exact counts against the APPROVED expectation; any structural defect blocks."""
    if not exp or not exp.get("approved_by"):
        return ["no approved inventory expectation — a bundle cannot be produced without one"]
    out = list(proj.get("structural_defects") or [])
    c = proj.get("counts") or {}
    if c.get("configured") != exp.get("accounts"):
        out.append(f"unique identity-bound accounts {c.get('configured')} != approved {exp.get('accounts')}")
    if c.get("live_enabled") != exp.get("enabled"):
        out.append(f"enabled accounts {c.get('live_enabled')} != approved {exp.get('enabled')}")
    if c.get("bots_enabled") != exp.get("bots"):
        out.append(f"enabled bots {c.get('bots_enabled')} != approved {exp.get('bots')}")
    if c.get("bots_enabled") != c.get("live_enabled"):
        out.append("exactly one enabled bot per enabled account required")
    out += [v for v in (proj.get("violations") or []) if v not in out and ("orphan" in v or "outside" in v or "disabled" in v)]
    return out


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
    # A14-9 — the approved inventory expectation + approver travel INSIDE the signed bundle
    from inventory_projection import projection
    exp = await db.platform_state.find_one({"_id": "inventory_expectation"}) or {}
    if not exp.get("approved_by"):
        raise RuntimeError("no approved inventory expectation — approve the inventory before building a bundle")
    proj = await projection(db)
    payload = {"release": rel, "generated_at": now.isoformat(), "actor": actor,
               "inventory": {"accounts": await db.accounts.count_documents(base_q),
                             "enabled": len(accounts),
                             "bots_active": sum(1 for a in accounts for b in a["bots"] if b["active"]),
                             "observed": proj.get("counts"), "inventory_hash": proj.get("inventory_hash"),
                             "expected": {k: exp.get(k) for k in ("accounts", "enabled", "bots", "account_ids", "policy_version")},
                             "approved_by": exp.get("approved_by"), "approved_at": exp.get("set_at")},
               "accounts": accounts}
    failures = [f"{a['label']}: {f}" for a in accounts for f in a["failures"]]
    failures += [f"inventory: {f}" for f in inventory_failures(proj, exp)]
    if not any(a["trading_enabled"] for a in accounts):
        failures.append("no enabled live account to accept")
    from release_signing import key_id
    doc = {"bundle_id": uuid.uuid4().hex, "payload": payload,
           "algo": "ed25519", "schema_version": SCHEMA_VERSION, "key_id": key_id(),
           "config_fingerprint": await config_fingerprint(db, [a["account_id"] for a in accounts if a["trading_enabled"]], rel),
           "verdict": "PASS" if not failures else "FAIL",
           "failures": failures, "build_sha": rel["build_sha"], "image_digest": rel["image_digest"],
           "created_by": actor, "created_at": now.isoformat(),
           "expires_at": (now + timedelta(days=BUNDLE_TTL_DAYS)).isoformat(),
           "account_ids": [a["account_id"] for a in accounts if a["trading_enabled"]]}
    doc["digest"], doc["signature"] = _sign(doc)
    await db.acceptance_bundles.insert_one(dict(doc))
    doc.pop("_id", None)
    return doc


def bundle_covers(bundle: dict | None, account_id: str, rel: dict, now: datetime | None = None,
                  current_fingerprint: str | None = None) -> tuple[bool, str]:
    now = now or _now()
    if not bundle:
        return False, "no acceptance bundle"
    # A14-11 — event-driven expiry: the configuration the bundle vouched for must be unchanged
    if current_fingerprint is not None and bundle.get("config_fingerprint") != current_fingerprint:
        return False, "configuration changed since the acceptance bundle (accounts/bots/credentials/EA/release) — new bundle required"
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
    fp = None
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
        if fp is None:
            fp = await config_fingerprint(db, list((b or {}).get("account_ids") or []), rel) if b else ""
        env = attested_environment(acc)
        ok, why = bundle_covers(b, str(acc["_id"]), rel, current_fingerprint=fp)
        accounts.append({"account_id": str(acc["_id"]), "label": acc.get("display_name") or acc.get("label") or acc.get("broker"),
                         "environment": env, "covered": ok, "reason": why,
                         "gate_applies": env == "LIVE"})
    live = [a for a in accounts if a["gate_applies"]]
    return {"required": required(), "release": rel, "latest": public_b,
            "valid_for_release": bool(live) and all(a["covered"] for a in live),
            "reason": ("all live accounts covered" if live and all(a["covered"] for a in live)
                       else (next((a["reason"] for a in live if not a["covered"]), None) or "no live account enabled")),
            "accounts": accounts}
