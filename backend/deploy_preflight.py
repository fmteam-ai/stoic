"""iter-181 — Deploy preflight: evaluates every APP_ENV=production startup
guardrail against this process's environment, so admins see exactly which
Secrets must be set before publishing (deploys never bounce on boot).

Each check answers: "would THIS value pass the production guardrail?"
In preview, values reflect what production will inherit from the codebase
.env — Secrets-tab overrides take precedence there and cannot be seen here.
"""
import os

from app_env import is_production


def _mask(v: str, keep: int = 4) -> str:
    if not v:
        return ""
    return v[:keep] + "•" * min(len(v) - keep, 12) if len(v) > keep else "•" * len(v)


def _check(cid, label, status, current, required, fix):
    return {"id": cid, "label": label, "status": status,
            "current": current, "required": required, "fix": fix}


def run_preflight() -> dict:
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

    for key in ("STEP_UP_BYPASS_TOKEN", "RATE_LIMIT_BYPASS_TOKEN"):
        val = env.get(key) or ""
        checks.append(_check(
            key.lower(), key, "pass" if not val else "fail",
            "set (" + _mask(val) + ")" if val else "(empty)", "(empty)",
            f"Clear {key} in the Secrets tab — test bypass secrets are "
            "forbidden in production and refuse boot."))

    mfa = env.get("ADMIN_MFA_ENFORCED", "true").strip().lower()
    checks.append(_check(
        "admin_mfa", "ADMIN_MFA_ENFORCED", "pass" if mfa == "true" else "fail",
        mfa, "true",
        "Set ADMIN_MFA_ENFORCED=true — production refuses ADMIN_MFA_ENFORCED"
        "=false. Admins must enroll TOTP 2FA."))

    pw = env.get("ADMIN_PASSWORD") or ""
    if pw and pw != "admin123" and len(pw) >= 12:
        pw_status, pw_cur = "pass", f"set ({len(pw)} chars)"
    else:
        pw_status = "fail"
        pw_cur = ("(not set)" if not pw
                  else "default admin123" if pw == "admin123"
                  else f"too short ({len(pw)} chars)")
    checks.append(_check(
        "admin_password", "ADMIN_PASSWORD", pw_status, pw_cur,
        "strong, ≥12 chars, not admin123",
        "Set a strong ADMIN_PASSWORD in Secrets — the default 'admin123' and "
        "passwords under 12 characters refuse boot in production."))

    ed = env.get("ED25519_SIGNING_KEY_B64") or ""
    checks.append(_check(
        "ed25519", "ED25519_SIGNING_KEY_B64", "pass" if ed else "fail",
        "set (" + _mask(ed) + ")" if ed else "(not set)", "set",
        "Required for release manifest signing — refuses boot when missing."))

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

    signer = env.get("RELEASE_SIGNER", "local").strip().lower()
    allow_local = env.get("RELEASE_SIGNER_ALLOW_LOCAL_IN_PROD",
                          "").strip().lower() == "true"
    if signer == "external":
        s_status, s_cur = "pass", "external"
    elif allow_local:
        # v56: the escape hatch was REMOVED — a set override is now a
        # misconfiguration signal, not a permission
        s_status, s_cur = "fail", ("local (RELEASE_SIGNER_ALLOW_LOCAL_IN_"
                                   "PROD is no longer supported)")
    else:
        # review P1-9: local signing in production is a FAIL, not a warning
        s_status, s_cur = "fail", "local (KMS/HSM required to sign in prod)"
    checks.append(_check(
        "release_signer", "RELEASE_SIGNER", s_status, s_cur,
        "external (KMS/HSM) — local signing is never permitted in "
        "production",
        "BLOCKS production boot (review P1-9): the API refuses to start "
        "with RELEASE_SIGNER=local when APP_ENV=production. Configure "
        "RELEASE_SIGNER=external with RELEASE_SIGNER_URL/TOKEN."))

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
