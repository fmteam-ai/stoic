#!/usr/bin/env python3
"""CI only — enrol a freshly generated TOTP secret on the per-run CI admin so the
Playwright suite exercises admin pages WITH admin MFA enforced (main92 follow-up).

Reads MONGO_URL / DB_NAME / ADMIN_EMAIL from the environment, writes the secret the
same way /api/auth/2fa/verify does (`totp_secret` + `two_factor_enabled`), and prints
the base32 secret on stdout (the caller masks it). Refuses to run against a database
whose name does not look like a CI/e2e database, and refuses under APP_ENV=production.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))

CI_DB_MARKERS = ("ci", "e2e", "test")
# main93 Low (M7) — hard guards: the script only runs INSIDE GitHub Actions (or with an explicit
# local opt-in), never against a production-looking database or a non-loopback/non-docker Mongo.
LOCAL_OPT_IN = "STOIC_ALLOW_LOCAL_TOTP_ENROL"


def _guard() -> str | None:
    if (os.environ.get("APP_ENV") or "").lower() == "production":
        return "APP_ENV=production"
    if os.environ.get("GITHUB_ACTIONS") != "true" and os.environ.get(LOCAL_OPT_IN) != "1":
        return f"not running in GitHub Actions (set {LOCAL_OPT_IN}=1 to opt in locally)"
    db_name = os.environ.get("DB_NAME") or ""
    if not any(m in db_name.lower() for m in CI_DB_MARKERS):
        return f"DB_NAME={db_name!r} does not look like a CI/e2e database"
    if db_name.lower() in ("ai_trading_bot", "stoic", "production", "prod"):
        return f"DB_NAME={db_name!r} is a production database name"
    url = os.environ.get("MONGO_URL") or ""
    host = url.split("@")[-1].split("/")[0].split(":")[0].lower()
    if host not in ("localhost", "127.0.0.1", "mongo", "mongodb", "::1"):
        return f"MONGO_URL host {host!r} is not a local/CI Mongo"
    return None


def main() -> int:
    why = _guard()
    if why:
        print(f"refusing: {why}", file=sys.stderr)
        return 2
    db_name = os.environ.get("DB_NAME") or ""
    email = (os.environ.get("ADMIN_EMAIL") or "").lower()
    if not email:
        print("refusing: ADMIN_EMAIL unset", file=sys.stderr)
        return 2
    from pymongo import MongoClient
    from totp import new_secret
    secret = new_secret()
    db = MongoClient(os.environ["MONGO_URL"])[db_name]
    res = db.users.update_one(
        {"email": email, "role": "admin"},
        {"$set": {"totp_secret": secret, "two_factor_enabled": True, "mfa_enrolled_by": "ci_enrol_admin_totp"},
         "$unset": {"totp_secret_pending": ""}})
    if not res.matched_count:
        print(f"refusing: no admin user {email} (start the backend first so seed.py creates it)", file=sys.stderr)
        return 1
    print(secret)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
