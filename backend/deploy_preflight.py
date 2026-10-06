"""iter-181 — Deploy preflight: evaluates every APP_ENV=production startup
guardrail against this process's environment, so admins see exactly which
Secrets must be set before publishing (deploys never bounce on boot).

Each check answers: "would THIS value pass the production guardrail?"
In preview, values reflect what production will inherit from the codebase
.env — Secrets-tab overrides take precedence there and cannot be seen here.
"""
import base64
import binascii
import os

from app_env import is_production, removable_secret


def _mask(v: str, keep: int = 0) -> str:
    """Length-only mask — never echoes secret characters (audit P3)."""
    if not v:
        return ""
    return "•" * min(len(v), 12) + f" ({len(v)} chars)"


def _mongo_transactions_supported(env):
    try:
        from pymongo import MongoClient
        c = MongoClient(env["MONGO_URL"], serverSelectionTimeoutMS=2000)
        hello = c.admin.command("hello")
        return bool(hello.get("setName")) or hello.get("msg") == "isdbgrid"
    except Exception:  # noqa: BLE001
        return None


def _check(cid, label, status, current, required, fix):
    return {"id": cid, "label": label, "status": status,
            "current": current, "required": required, "fix": fix}


def run_preflight(check_signer_health: bool = False) -> dict:
    env = os.environ
    checks = []

    app_env = env.get("APP_ENV", "").strip()
    checks.append(_check(
        "app_env", "APP_ENV", "pass" if is_production() else "warn",
        app_env or "(not set)", "production",
        "Set APP_ENV=production in the publish panel Secrets tab. Without it "
        "NONE of the guardrails below are enforced."))

    csrf = env.get("CSRF_ENFORCE_ORIGIN", "").strip().lower()
    checks.append(_check(
        "csrf", "CSRF origin enforcement", "pass",
        "auto-enforced in production"
        + (f" (env: {csrf})" if csrf else ""),
        "enforced",
        "No secret needed — production always enforces the Origin allowlist "
        "(CSRF_ENFORCE_ORIGIN only matters in preview)."))

    def _prod_filtered(origins):
        return {o for o in origins
                if "localhost" not in o and "127.0.0.1" not in o
                and ".preview.emergentagent.com" not in o}

    from security import _allowed_origins
    raw_origins = _allowed_origins()
    prod_origins = _prod_filtered(raw_origins)
    checks.append(_check(
        "cors", "CORS_ORIGINS", "pass" if prod_origins else "fail",
        (", ".join(sorted(prod_origins))
         + " (localhost/preview entries auto-ignored in production)")
        if raw_origins else "(empty / *)",
        "at least one real production origin",
        "Production refuses to boot when CORS_ORIGINS has no real "
        "production origin after localhost/preview entries are filtered "
        "out. The codebase .env already includes the stoicaibot.com "
        "domains — no Secret needed unless you change domains."))

    from app_env import bypass_token
    for key in ("STEP_UP_BYPASS_TOKEN", "RATE_LIMIT_BYPASS_TOKEN"):
        val = bypass_token(key)
        checks.append(_check(
            key.lower(), key, "pass" if not val else "fail",
            "set (" + _mask(val) + ")" if val else "(empty / disabled)", "disabled",
            f"Set {key}=disabled in the Secrets tab (the panel cannot save an "
            "empty value) — test bypass secrets are forbidden in production and refuse boot."))

    mfa = env.get("ADMIN_MFA_ENFORCED", "true").strip().lower()
    checks.append(_check(
        "admin_mfa", "ADMIN_MFA_ENFORCED", "pass" if mfa == "true" else "fail",
        mfa, "true",
        "Set ADMIN_MFA_ENFORCED=true — production refuses ADMIN_MFA_ENFORCED"
        "=false. Admins must enroll TOTP 2FA."))

    # A13-2 — the deploy preflight mirrors the boot guard so update.sh refuses BEFORE the restart
    from bridge_tokens import production_key_violation
    _bk = production_key_violation(env)
    checks.append(_check(
        "bridge_hash_key", "BRIDGE_TOKEN_HASH_KEY", "pass" if not _bk else "fail",
        "set" if (env.get("BRIDGE_TOKEN_HASH_KEY") or "").strip() else "unset", "32+ random chars, ≠ JWT_SECRET",
        _bk or "ok"))

    # A13 P0-02 — the deploy must be an authoritative, digest-pinned, signed release
    from release_gate import evaluate as _release_eval
    _rg = _release_eval(env=env)
    checks.append(_check(
        "release_gate", "Authoritative release (rc_lock + digests + signed EX5)",
        "pass" if _rg["ok"] else ("fail" if is_production() else "warn"),
        "ok" if _rg["ok"] else "; ".join(_rg["failures"][:2]),
        "release.yml-generated lock, STOIC_IMAGE_DIGEST == locked backend digest, signed EX5 record",
        "Run the release workflow on a signed tag and deploy by digest — live authority stays CLOSE_ONLY otherwise."))

    pw = env.get("ADMIN_PASSWORD") or ""
    from seed import _is_known_default_password
    if pw and not _is_known_default_password(pw) and len(pw) >= 12:
        pw_status, pw_cur = "pass", f"set ({len(pw)} chars)"
    else:
        pw_status = "fail"
        pw_cur = ("(not set)" if not pw
                  else "well-known default password" if _is_known_default_password(pw)
                  else f"too short ({len(pw)} chars)")
    checks.append(_check(
        "admin_password", "ADMIN_PASSWORD", pw_status, pw_cur,
        "strong, ≥12 chars, not a well-known default",
        "Set a strong ADMIN_PASSWORD in Secrets — well-known defaults and "
        "passwords under 12 characters refuse boot in production."))

    # security audit #7 SEC-001 — key separation (mirrors the boot guard)
    jwt = env.get("JWT_SECRET") or ""
    for cid, name in (("order_auth_key", "ORDER_AUTH_SECRET"), ("ledger_anchor_key", "LEDGER_ANCHOR_KEY")):
        val = env.get(name) or ""
        ok = bool(val) and val != jwt
        checks.append(_check(
            cid, name, "pass" if ok else "fail",
            ("set (" + _mask(val) + ")" if val else "(not set)") + ("" if ok or not val else " — equals JWT_SECRET"),
            "set, distinct from JWT_SECRET",
            f"Give {name} its own random 32+ byte value — signers must never fall back to the auth secret; "
            "production refuses boot otherwise."))

    kvm = env.get("KEY_VAULT_MASTER") or ""
    checks.append(_check(
        "key_vault", "KEY_VAULT_MASTER", "pass" if kvm else "fail",
        "set (" + _mask(kvm) + ")" if kvm else "(not set)",
        "set (distinct from JWT_SECRET)",
        "secrets_vault refuses to run in production without its own master "
        "key (broker credentials / command keys are encrypted with it)."))

    # audit r28 P2-02 — integrations vault key must be dedicated (never HKDF(JWT_SECRET)) in production
    smk = env.get("SECRETS_MASTER_KEY") or ""
    smk_ok = False
    try:
        smk_ok = bool(smk) and len(base64.b64decode(smk, validate=True)) == 32 and smk != jwt and smk != kvm
    except (ValueError, binascii.Error):
        smk_ok = False
    checks.append(_check(
        "secrets_master_key", "SECRETS_MASTER_KEY", "pass" if smk_ok else "fail",
        ("set (" + _mask(smk) + ")" if smk else "(not set)") + ("" if smk_ok or not smk else " — not 32 bytes base64 / not distinct"),
        "32 random bytes, base64, distinct from JWT_SECRET and KEY_VAULT_MASTER",
        "deploy/install.sh and deploy/update.sh generate secrets/secrets_master_key automatically "
        "(compose → SECRETS_MASTER_KEY_FILE); production boot refuses without it."))

    # audit round 9 P1-01 — the SAME complete validator the boot guard and signer use
    from release_signing import signer_config_violations, signer_health, _mode as _signer_mode
    _viols = signer_config_violations(env)
    _mode = _signer_mode(env)
    checks.append(_check(
        "release_signer", "RELEASE_SIGNER configuration",
        ("warn" if _mode == "deferred" else "pass") if not _viols else "fail",
        (_mode + " (boots; nothing is signed; live exposure CLOSE_ONLY until an external signer is configured)"
         if _mode == "deferred" else _mode) if not _viols else f"{_mode}: " + " | ".join(_viols),
        "external (KMS/HSM) with https URL in RELEASE_SIGNER_ALLOWED_HOSTS, RELEASE_SIGNER_TOKEN, "
        "pinned RELEASE_PUBLIC_KEY_B64, RELEASE_SIGNER_KEY_ID, RELEASE_SIGNER_TIMEOUT 1-30 s; "
        "NO ED25519_SIGNING_KEY_B64 in the API",
        "BLOCKS production boot: the API refuses to start unless the signer configuration is "
        "complete and the private key is absent (release_signing.signer_config_violations)."))
    checks.append(_check(
        "ed25519", "ED25519_SIGNING_KEY_B64",
        "pass" if not removable_secret(env, "ED25519_SIGNING_KEY_B64") else "fail",
        "absent / disabled" if not removable_secret(env, "ED25519_SIGNING_KEY_B64") else "PRESENT",
        "absent in production (external signer holds the key)",
        "Set ED25519_SIGNING_KEY_B64=disabled in the Secrets tab (the panel refuses empty values); sign via RELEASE_SIGNER=external."))
    # N101-5 — the trading API must hold ONLY the bundle token: with the CI release token it could mint EA-release signatures
    _rel_tok = bool(removable_secret(env, "RELEASE_SIGNER_TOKEN"))
    _bun_tok = bool((env.get("RELEASE_SIGNER_BUNDLE_TOKEN") or "").strip())
    checks.append(_check(
        "release_signer_token_scope", "Signer token held by the API",
        ("fail" if _rel_tok else ("pass" if _bun_tok else "warn")) if _mode == "external" else "pass",
        ("RELEASE_SIGNER_TOKEN PRESENT (release token on the API host)" if _rel_tok
         else "bundle token only" if _bun_tok else "no signer token (runtime signing unavailable)") if _mode == "external" else _mode,
        "RELEASE_SIGNER_BUNDLE_TOKEN only; RELEASE_SIGNER_TOKEN (CI release token) absent from backend/.env",
        "deploy/update.sh strips RELEASE_SIGNER_TOKEN from backend/.env; compose mounts secrets/signer_token_bundle as "
        "RELEASE_SIGNER_BUNDLE_TOKEN_FILE. The release token belongs to CI (GitHub secret) and the signer only."))
    # N102-5 — the local runtime signer must never BE the release key: with one key, root on the API host
    # could mint EA-release signatures that verify. FAIL in production when the two pins are the same key.
    _rp = (env.get("RELEASE_PUBLIC_KEY_B64") or "").strip(); _bp = (env.get("BUNDLE_PUBLIC_KEY_B64") or "").strip()
    _same = bool(_rp) and _rp == _bp
    checks.append(_check(
        "release_key_distinct", "CI release key ≠ runtime bundle key",
        ("fail" if is_production() else "warn") if _same else ("pass" if (_rp and _bp) else "warn"),
        "IDENTICAL pins" if _same else ("distinct" if (_rp and _bp) else
                                         ("release pin missing — EA records / model manifests cannot verify" if not _rp
                                          else "bundle pin missing — single-key install")),
        "RELEASE_PUBLIC_KEY_B64 = CI signer (Fly) key; BUNDLE_PUBLIC_KEY_B64 = local sidecar key; never the same key",
        "Re-key the CI signer (deploy/signer/deploy_fly.sh), re-run ea-release, pin its public key as RELEASE_PUBLIC_KEY_B64; "
        "the sidecar key stays BUNDLE_PUBLIC_KEY_B64 (deploy/lib.sh ensure_bundle_key_pins)."))
    if check_signer_health and not _viols and _mode == "external":
        import time as _t
        _t0 = _t.monotonic()
        h = signer_health(env)
        _p = __import__("urllib.parse").parse.urlparse(env.get("RELEASE_SIGNER_URL", ""))
        c = _check(
            "release_signer_health", "External signer identity/health",
            "pass" if h["ok"] else "fail",
            "ok" if h["ok"] else h.get("error", "unreachable"),
            "GET {RELEASE_SIGNER_URL}/health → ok + key_id + public_key_b64 matching the pinned identity",
            "Signer unreachable or identity mismatch blocks attestations and promotion (read-only service health is unaffected).")
        c["signer"] = {"mode": "external", "host": _p.hostname, "remote_key_id": h.get("remote_key_id"),
                       "pinned_key_id": h.get("key_id"), "identity_matches": h.get("identity_matches"),
                       "pinned_public_key_prefix": (env.get("RELEASE_PUBLIC_KEY_B64") or "").strip()[:12],
                       "latency_ms": round((_t.monotonic() - _t0) * 1000), "error": h.get("error")}
        checks.append(c)
    elif check_signer_health:
        c = _check(
            "release_signer_health", "External signer identity/health",
            "warn" if not _viols else "fail",
            f"{_mode} — no remote signer to probe" if not _viols else "not probed (configuration invalid)",
            "RELEASE_SIGNER=external reachable with matching key_id + pinned public key",
            "Configure the external signer (docs/PRODUCTION_DEPLOY_CHECKLIST.md Step 1) — the probe runs once RELEASE_SIGNER=external is valid.")
        c["signer"] = {"mode": _mode, "host": None, "remote_key_id": None, "pinned_key_id": None,
                       "identity_matches": None, "pinned_public_key_prefix": None, "latency_ms": None,
                       "error": _viols[0] if _viols else None}
        checks.append(c)

    # r17 P0-01 — fenced NL effects commit in ONE MongoDB transaction in production
    txn = _mongo_transactions_supported(env)
    checks.append(_check(
        "mongo_transactions", "MongoDB transactions (replica set)",
        "pass" if txn is True else ("fail" if is_production() else "warn"),
        {True: "replica set — transactions available", False: "standalone — NO transactions",
         None: "unknown (MongoDB unreachable)"}[txn],
        "replica set (single-node rs0 is fine) so fenced NL effects commit atomically",
        "Production NL effects FAIL CLOSED without transactions: run MongoDB with --replSet rs0 "
        "(+ keyFile with auth) and rs.initiate() — docs/PRODUCTION_DEPLOY_CHECKLIST.md."))

    workers = env.get("BACKGROUND_WORKERS_IN_PROCESS")
    checks.append(_check(
        "workers", "BACKGROUND_WORKERS_IN_PROCESS",
        "pass" if workers is not None else "warn",
        workers if workers is not None else "(not set)",
        "true (single process) or false (dedicated workers)",
        "Unset means embedded trading/protection loops are DISABLED "
        "(fail-safe). Set it explicitly."))

    ts_site, ts_secret = env.get("TURNSTILE_SITE_KEY"), env.get("TURNSTILE_SECRET_KEY")
    checks.append(_check(
        "turnstile", "TURNSTILE keys",
        "pass" if (ts_site and ts_secret) else "warn",
        "both set" if (ts_site and ts_secret) else "missing",
        "site + secret from the SAME Cloudflare widget",
        "Optional — only needed if the Turnstile login gate is enabled."))

    fails = [c["id"] for c in checks if c["status"] == "fail"]
    warns = [c["id"] for c in checks if c["status"] == "warn"]
    verdict = ("will_crash" if fails
               else "ready_with_warnings" if warns else "ready")
    return {"environment": "production" if is_production() else "preview",
            "verdict": verdict, "fail_count": len(fails),
            "warn_count": len(warns), "checks": checks,
            "note": ("Values shown are what production inherits from the "
                     "codebase .env — keys overridden in the publish panel's "
                     "Secrets tab take precedence there. Every FAIL must be "
                     "overridden in Secrets or the deploy will bounce.")}
