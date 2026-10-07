#!/usr/bin/env python3
"""A15-1 — mint a SIGNED inventory policy migration (purpose `policy-migration`, CI release key).

Run by .github/workflows/policy-migration.yml (GitHub environment `policy-approval` = ONE reviewer approval
gates the signing run; the independent second approval is the admin's propose/approve on the server — N104-5)
or by an operator holding RELEASE_SIGNER_TOKEN. Writes release/policy_migrations/<policy_version>.json;
the Inventory Go-Live panel loads that file and submits it with the expectation.

  python scripts/sign_policy_migration.py --installation-id <id> --environment production \
      --previous 6/3/3-v1 --version demo-2x2-v1 --accounts 2 --enabled 2 --bots 2 \
      --account-ids <uuid>,<uuid> --demo-only --reason "4-week MT5 demo on two attested demo accounts" \
      --issuer ops@example.com --expires-days 45
"""
import argparse
import json
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))


def build(args) -> dict:
    now = datetime.now(timezone.utc)
    ids = sorted(i.strip() for i in args.account_ids.split(",") if i.strip())
    if len(ids) != args.enabled or args.bots != args.enabled or args.enabled > args.accounts:
        raise SystemExit("counts must satisfy len(account_ids) == enabled == bots <= accounts")
    if len(args.reason.strip()) < 10 or not args.issuer.strip():
        raise SystemExit("reason (>=10 chars) and issuer are required")
    import re
    from inventory_projection import (POLICY_VERSION_RE, PREVIOUS_POLICY_VERSION_RE, DEMO_POLICY_DEFAULT_DAYS,
                                      DEMO_POLICY_MAX_DAYS, POLICY_MAX_DAYS)   # A16-3 — one rule per field, shared with workflow + server
    if not re.fullmatch(POLICY_VERSION_RE, args.version):
        raise SystemExit(f"policy_version must match {POLICY_VERSION_RE} (it becomes the file name — no '/')")
    if not re.fullmatch(PREVIOUS_POLICY_VERSION_RE, args.previous):
        raise SystemExit(f"previous_policy_version must match {PREVIOUS_POLICY_VERSION_RE}")
    # A16-4 — demo policies are short-lived: default 30 days, at most 45
    if args.expires_days is None:
        args.expires_days = DEMO_POLICY_DEFAULT_DAYS if args.demo_only else 45
    if args.demo_only and not (1 <= args.expires_days <= DEMO_POLICY_MAX_DAYS):
        raise SystemExit(f"demo_only policies may be valid for 1..{DEMO_POLICY_MAX_DAYS} days (got {args.expires_days})")
    if not (1 <= args.expires_days <= POLICY_MAX_DAYS):
        raise SystemExit(f"expires_days must be 1..{POLICY_MAX_DAYS}")
    return {"schema": "stoic.policy-migration/v3", "installation_id": args.installation_id,
            "environment": args.environment, "previous_policy_version": args.previous,
            "policy_version": args.version, "accounts": args.accounts, "enabled": args.enabled, "bots": args.bots,
            "account_ids": ids, "demo_only": bool(args.demo_only), "reason": args.reason.strip(),
            "issuer": args.issuer.strip(), "issued_at": now.isoformat(),
            "expires_at": (now + timedelta(days=args.expires_days)).isoformat(), "nonce": secrets.token_hex(16)}


def sign(mig: dict) -> dict:
    from inventory_projection import migration_body
    from release_signing import key_id, sign_hex
    mig["signature_hex"] = sign_hex(migration_body(mig), purpose="policy-migration")
    mig["key_id"] = key_id(purpose="policy-migration")
    return mig


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--installation-id", required=True)
    ap.add_argument("--environment", default="production", choices=["production", "preview"])
    ap.add_argument("--previous", required=True, help="policy version currently in force (e.g. 6/3/3-v1)")
    ap.add_argument("--version", required=True, help="new policy version (e.g. demo-2x2-v1)")
    ap.add_argument("--accounts", type=int, required=True)
    ap.add_argument("--enabled", type=int, required=True)
    ap.add_argument("--bots", type=int, required=True)
    ap.add_argument("--account-ids", required=True, help="comma-separated platform account ids")
    ap.add_argument("--demo-only", action="store_true", help="every listed account must be an attested DEMO account")
    ap.add_argument("--reason", required=True)
    ap.add_argument("--issuer", required=True)
    ap.add_argument("--expires-days", type=int, default=None, help="default 30 for --demo-only (max 45), else 45")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    mig = sign(build(a))
    out = a.out or os.path.join(ROOT, "release", "policy_migrations", f"{a.version}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(mig, fh, indent=2, sort_keys=True)
        fh.write("\n")
    print(f"signed policy migration {a.version} ({'DEMO-only' if a.demo_only else 'LIVE'}) → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
