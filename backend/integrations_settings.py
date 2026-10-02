"""Admin → Integrations: status, live credential tests and sealed in-UI secret updates.

Posture (round 10 P1-04 kept): process configuration still comes from the
environment. The sealed vault (`secrets_vault`) is an OVERLAY: values are
AES-256-GCM encrypted with a server-side master key (SECRETS_MASTER_KEY, else
HKDF(JWT_SECRET)), loaded into os.environ at process start and applied to the
API process immediately on update. Every update requires admin re-auth
(password + TOTP when enabled) and is appended to the hash-chained admin audit log.
"""
import base64
import hashlib
import logging
import os
from datetime import datetime, timezone

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# key → (provider, secret?, description)
REGISTRY = {
    "STRIPE_API_KEY": ("stripe", True, "Secret API key (sk_live_… / sk_test_…)"),
    "STRIPE_WEBHOOK_SECRET": ("stripe", True, "Webhook signing secret (whsec_…) for /api/webhook/stripe"),
    "TURNSTILE_SITE_KEY": ("turnstile", False, "Cloudflare Turnstile site key (public)"),
    "TURNSTILE_SECRET_KEY": ("turnstile", True, "Cloudflare Turnstile secret key"),
    "TURNSTILE_EXPECTED_HOSTNAMES": ("turnstile", False, "Comma-separated hostnames the widget is bound to"),
    "TURNSTILE_LOGIN_DEGRADED_POLICY": ("turnstile", False, "closed (default) | otp_required — login behaviour when Cloudflare is unreachable; never fail-open"),
    "RESEND_API_KEY": ("email", True, "Resend API key (re_…)"),
    "SENDER_EMAIL": ("email", False, "From address on a verified Resend domain"),
    "SENDER_NAME": ("email", False, "Display name on outgoing e-mail"),
    "EMERGENT_LLM_KEY": ("ai", True, "Emergent Universal Key (Co-Pilot, Risk Commander, AI agents)"),
}
PROVIDERS = {
    "stripe": "Stripe payments", "turnstile": "Cloudflare Turnstile", "email": "E-mail (Resend)", "ai": "AI (Emergent)"}
_AAD = b"stoic-secrets-vault-v1"
DECRYPT_FAILURES: list[str] = []   # keys whose sealed value could not be unsealed at boot (master-key mismatch)


def _production() -> bool:
    return (os.environ.get("APP_ENV") or "").lower() == "production"


def _master_key() -> bytes:
    raw = os.environ.get("SECRETS_MASTER_KEY")
    if raw:
        k = base64.b64decode(raw)
        if len(k) != 32:
            raise RuntimeError("SECRETS_MASTER_KEY must be 32 bytes base64")
        return k
    if _production():
        # audit r28 P2-02 — never couple vault encryption to the JWT signing secret in production
        raise RuntimeError("APP_ENV=production requires a dedicated SECRETS_MASTER_KEY (32 bytes base64) — "
                           "the vault must not derive from JWT_SECRET")
    seed = os.environ.get("JWT_SECRET")
    if not seed:
        raise RuntimeError("no SECRETS_MASTER_KEY / JWT_SECRET — vault unavailable")
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"stoic-vault", info=_AAD).derive(seed.encode())


def master_key_source() -> str:
    return "dedicated" if os.environ.get("SECRETS_MASTER_KEY") else "derived_from_jwt_secret"


def _legacy_derived_key() -> bytes | None:
    """Pre-r28 vault key (HKDF of JWT_SECRET) — read-only, used ONCE to migrate legacy records."""
    seed = os.environ.get("JWT_SECRET")
    if not seed:
        return None
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"stoic-vault", info=_AAD).derive(seed.encode())


def _migrate_legacy_record(col, doc: dict) -> str | None:
    """Record sealed before dedicated keys existed (no key_version): open with the
    legacy derived key and re-seal under the current dedicated key in place.
    Compare-and-set on the old ciphertext so a concurrent writer is never clobbered."""
    if doc.get("key_version") is not None or not os.environ.get("SECRETS_MASTER_KEY"):
        return None
    legacy = _legacy_derived_key()
    if legacy is None:
        return None
    try:
        plain = _unseal_with(legacy, doc)
    except Exception:  # noqa: BLE001
        return None
    new = {**seal(plain), "migrated_from": "jwt_derived",
           "migrated_at": datetime.now(timezone.utc).isoformat()}
    col.update_one({"_id": doc["_id"], "ciphertext": doc["ciphertext"]}, {"$set": new})
    logging.getLogger("secrets").warning("vault: migrated legacy record %s to dedicated master key", doc["_id"])
    return plain


# ── audit r29 P2-04: versioned keys · dual-read / single-write · transactional rewrap ──
def key_version() -> int:
    return int(os.environ.get("SECRETS_MASTER_KEY_VERSION") or 1)


def _previous_key() -> bytes | None:
    raw = os.environ.get("SECRETS_MASTER_KEY_PREVIOUS")
    if not raw:
        return None
    k = base64.b64decode(raw)
    if len(k) != 32:
        raise RuntimeError("SECRETS_MASTER_KEY_PREVIOUS must be 32 bytes base64")
    return k


def _unseal_with(key: bytes, doc: dict) -> str:
    return AESGCM(key).decrypt(base64.b64decode(doc["nonce"]), base64.b64decode(doc["ciphertext"]), _AAD).decode()


async def rewrap_all(db, actor: dict) -> dict:
    """Re-seal every vault record under the CURRENT key (reads with current, then
    previous). Writes a rewrap manifest first, verifies completeness (every record
    re-openable with the current key), and flips the manifest to `complete` —
    an incomplete manifest is a readiness blocker until resolved."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    docs = await db.secrets_vault.find({}).to_list(500)
    manifest = {"_id": f"rewrap_{now}", "status": "running", "started_at": now, "actor": actor.get("email"),
                "to_key_version": key_version(), "to_key_id": master_key_id(),
                "total": len(docs), "rewrapped": [], "failed": []}
    await db.secrets_rewrap_manifests.insert_one(dict(manifest))
    cur, prev = _master_key(), _previous_key()
    for d in docs:
        plain = None
        for k in (cur, prev):
            if k is None:
                continue
            try:
                plain = _unseal_with(k, d)
                break
            except Exception:  # noqa: BLE001
                continue
        if plain is None:
            manifest["failed"].append(d["_id"])
            continue
        new = {**seal(plain), "key_version": key_version(), "rewrapped_at": now, "rewrapped_by": actor.get("email")}
        await db.secrets_vault.update_one({"_id": d["_id"]}, {"$set": new})
        manifest["rewrapped"].append(d["_id"])
    # completeness verification: every record must open with the CURRENT key alone
    unverifiable = []
    for d in await db.secrets_vault.find({}).to_list(500):
        try:
            _unseal_with(cur, d)
        except Exception:  # noqa: BLE001
            unverifiable.append(d["_id"])
    manifest["failed"] = sorted(set(manifest["failed"]) | set(unverifiable))
    manifest["status"] = "complete" if not manifest["failed"] else "incomplete"
    manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
    await db.secrets_rewrap_manifests.replace_one({"_id": manifest["_id"]}, manifest)
    DECRYPT_FAILURES[:] = manifest["failed"]
    return {k: v for k, v in manifest.items() if k != "_id"} | {"manifest_id": manifest["_id"]}


async def rewrap_readiness(db) -> dict:
    """Latest rewrap manifest must be complete and every worker must have booted on the current key id."""
    m = await db.secrets_rewrap_manifests.find_one({}, sort=[("started_at", -1)])
    if not m:
        return {"ok": True, "detail": "no rewrap in progress"}
    acks = await db.worker_leases.find({}, {"_id": 1, "vault_key_id": 1}).to_list(50)
    stale = [str(a["_id"]) for a in acks if a.get("vault_key_id") and a["vault_key_id"] != m["to_key_id"]]
    ok = m["status"] == "complete" and not stale
    return {"ok": ok, "manifest_id": m["_id"], "status": m["status"], "failed": m.get("failed", []),
            "workers_on_old_key": stale,
            "detail": "rewrap complete, all workers acknowledged" if ok else
                      f"rewrap {m['status']}; failed={len(m.get('failed', []))}; workers on old key={len(stale)}"}


def master_key_id() -> str:
    return hashlib.sha256(_master_key()).hexdigest()[:12]


def seal(value: str) -> dict:
    nonce = os.urandom(12)
    ct = AESGCM(_master_key()).encrypt(nonce, value.encode(), _AAD)
    return {"nonce": base64.b64encode(nonce).decode(), "ciphertext": base64.b64encode(ct).decode(),
            "master_key_id": master_key_id(), "key_version": key_version()}


def unseal(doc: dict) -> str:
    """Dual-read: current key first, then SECRETS_MASTER_KEY_PREVIOUS during a rotation window.
    Writes always use the current key (seal())."""
    try:
        return _unseal_with(_master_key(), doc)
    except Exception:  # noqa: BLE001
        prev = _previous_key()
        if prev is None:
            raise
        return _unseal_with(prev, doc)


def tail(value: str) -> str:
    v = value or ""
    return ("…" + v[-4:]) if len(v) >= 8 else ("set" if v else "")


def load_vault_sync(mongo_url: str, db_name: str) -> int:
    """Process start (API + every worker): overlay sealed values onto os.environ.
    Records (never hides) sealed values the current master key cannot open."""
    from pymongo import MongoClient
    n = 0
    DECRYPT_FAILURES.clear()
    try:
        col = MongoClient(mongo_url, serverSelectionTimeoutMS=3000)[db_name].secrets_vault
        for doc in col.find({"_id": {"$in": list(REGISTRY)}}):
            try:
                os.environ[doc["_id"]] = unseal(doc)
                n += 1
            except Exception:  # noqa: BLE001 — wrong master key: keep env value, never crash boot
                plain = _migrate_legacy_record(col, doc)
                if plain is None:
                    DECRYPT_FAILURES.append(doc["_id"])
                    continue
                os.environ[doc["_id"]] = plain
                n += 1
    except Exception:  # noqa: BLE001
        return 0
    return n


def readiness_check() -> dict:
    """Release-readiness: sealed secrets must be openable and production must use a dedicated key."""
    problems = list(DECRYPT_FAILURES)
    src = master_key_source()
    if _production() and src != "dedicated":
        problems.append("SECRETS_MASTER_KEY missing — vault derived from JWT_SECRET")
    if _production() and os.environ.get("SECRETS_MASTER_KEY_PREVIOUS"):
        problems.append("SECRETS_MASTER_KEY_PREVIOUS still set — finish the rewrap and remove the old key")
    return {"ok": not problems, "master_key_source": src, "key_version": key_version(),
            "undecryptable_keys": list(DECRYPT_FAILURES),
            "detail": "; ".join(problems) or "all sealed secrets opened with the current master key"}


async def status(db) -> dict:
    vault = {d["_id"]: d async for d in db.secrets_vault.find({"_id": {"$in": list(REGISTRY)}})}
    keys = {}
    for k, (prov, secret, desc) in REGISTRY.items():
        v = os.environ.get(k) or ""
        src = ("vault_undecryptable" if k in DECRYPT_FAILURES
               else "vault" if k in vault else ("env" if v else "unset"))
        keys[k] = {"provider": prov, "secret": secret, "description": desc, "configured": bool(v) and k not in DECRYPT_FAILURES,
                   "source": src, "display": tail(v) if secret else v,
                   "updated_at": vault.get(k, {}).get("updated_at"), "updated_by": vault.get(k, {}).get("updated_by")}
    last_webhook = await db.stripe_webhook_events.find_one({}, sort=[("at", -1)])
    last_email = await db.email_log.find_one({}, sort=[("at", -1)])
    ts_state = await db.platform_state.find_one({"_id": "turnstile"})
    return {"keys": keys, "providers": PROVIDERS, "master_key_source": master_key_source(),
            "vault_readiness": readiness_check(),
            "signals": {"stripe_last_webhook": (last_webhook or {}).get("at"),
                        "stripe_last_webhook_type": (last_webhook or {}).get("type"),
                        "email_last_sent": (last_email or {}).get("at"),
                        "turnstile_policy_enabled": bool(ts_state and ts_state.get("enabled"))},
            "restart_hint": "Workers (6 containers) load vault values at start — run deploy/restart.sh after "
                            "changing keys used by background jobs; the API applies them immediately."}


async def update_secret(db, key: str, value: str, actor: dict) -> dict:
    if key not in REGISTRY:
        raise ValueError("unknown key")
    value = (value or "").strip()
    if len(value) > 4000 or "\n" in value:
        raise ValueError("invalid value")
    now = datetime.now(timezone.utc).isoformat()
    if value:
        doc = {**seal(value), "updated_at": now, "updated_by": actor.get("email"), "tail": tail(value)}
        await db.secrets_vault.update_one({"_id": key}, {"$set": doc}, upsert=True)
        os.environ[key] = value
    else:   # clearing removes the overlay → the .env value (if any) is authoritative again
        await db.secrets_vault.delete_one({"_id": key})
        from dotenv import dotenv_values
        env_val = (dotenv_values(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")) or {}).get(key)
        if env_val:
            os.environ[key] = env_val
        else:
            os.environ.pop(key, None)
    from audit_chain import append_chained
    await append_chained(db, {"actor_email": actor.get("email"), "action": "integration_secret_update",
                              "target_kind": "secret", "target_id": key, "target_label": REGISTRY[key][0],
                              "reason": "cleared" if not value else f"set ({tail(value)})", "meta": {"reauth": True},
                              "at": now})
    return {"ok": True, "key": key, "configured": bool(value), "display": tail(value) if REGISTRY[key][1] else value}


async def test_provider(db, provider: str, actor: dict) -> dict:
    if provider == "stripe":
        key = os.environ.get("STRIPE_API_KEY")
        if not key:
            return {"ok": False, "detail": "STRIPE_API_KEY not configured"}
        if key.endswith("emergent"):
            return {"ok": True, "detail": "Emergent-managed Stripe test key — checkout works through Emergent; claim your "
                                          "Stripe account and paste sk_live_… here to take real payments"}
        try:
            import stripe as stripe_sdk
            stripe_sdk.api_key = key
            acct = stripe_sdk.Account.retrieve()
            return {"ok": True, "detail": f"Stripe account {acct.get('id')} ({'live' if key.startswith('sk_live') else 'test'} mode)"
                                          + ("" if os.environ.get("STRIPE_WEBHOOK_SECRET") else " — webhook secret missing")}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "detail": f"Stripe rejected the key: {type(e).__name__}"}
    if provider == "email":
        from email_sender import send_email, is_configured
        if not is_configured():
            return {"ok": False, "detail": "RESEND_API_KEY not configured"}
        r = await send_email(actor["email"], "STOIC test e-mail", "<p>Your STOIC e-mail integration works.</p>",
                             text="Your STOIC e-mail integration works.")
        return {"ok": bool(r.get("ok")), "detail": f"sent to {actor['email']}" if r.get("ok") else str(r.get("error"))}
    if provider == "turnstile":
        import httpx
        from turnstile_gate import configuration_state
        state = configuration_state()
        if state != "ready":
            return {"ok": False, "detail": "Turnstile keys incomplete — set TURNSTILE_SITE_KEY and TURNSTILE_SECRET_KEY"}
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.post("https://challenges.cloudflare.com/turnstile/v0/siteverify",
                                 data={"secret": os.environ["TURNSTILE_SECRET_KEY"], "response": "stoic-probe"})
            codes = r.json().get("error-codes") or []
            if "invalid-input-secret" in codes:
                return {"ok": False, "detail": "Cloudflare rejected TURNSTILE_SECRET_KEY"}
            return {"ok": True, "detail": "secret accepted by Cloudflare (probe token correctly refused)"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "detail": f"Cloudflare unreachable: {type(e).__name__}"}
    if provider == "ai":
        if not os.environ.get("EMERGENT_LLM_KEY"):
            return {"ok": False, "detail": "EMERGENT_LLM_KEY not configured"}
        return {"ok": True, "detail": "key present — ask the Co-Pilot a question to exercise it"}
    raise ValueError("unknown provider")
