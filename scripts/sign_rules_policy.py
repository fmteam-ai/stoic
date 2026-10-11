#!/usr/bin/env python3
"""Phase 3 (U12/U13) — build and sign a RULES policy (schema stoic.policy-rules/v1).

Replaces the per-account `sign_policy_migration.py` round (workflow + release + two approvals per account):
sign the RULES once, commit the file under policies/ (the server activates it at startup — no release) or
upload it in Admin → Policy. Signed with the release key (purpose "policy-rules", release token / local key).

    python scripts/sign_rules_policy.py --version demo-rules-v1 --installation-id <id> --environment production \\
        --demo-only --max-accounts-per-user 1 --allowed-brokers '*' --expires-days 30 \\
        --reason "migrate demo-1x1-v1 to rules" --issuer ops@stoicaibot.com --out policies/demo-rules-v1.json

--migrate-from <release/policy_migrations/demo-1x1-v1.json> copies installation id / environment / issuer
from the old signed migration (equivalent rules: demo_only, 1 account per user, any registry broker, 30 days).
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))


def build(a) -> dict:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    pol = {"schema": "stoic.policy-rules/v1", "installation_id": a.installation_id, "environment": a.environment,
           "policy_version": a.version, "demo_only": bool(a.demo_only), "max_accounts_per_user": int(a.max_accounts_per_user),
           "allowed_brokers": [b.strip() for b in a.allowed_brokers.split(",") if b.strip()],
           "risk_caps": {k: v for k, v in (("max_risk_pct", a.max_risk_pct), ("max_daily_loss_pct", a.max_daily_loss_pct)) if v is not None},
           "reason": a.reason.strip(), "issuer": a.issuer, "issued_at": now.isoformat(),
           "expires_at": (now + timedelta(days=int(a.expires_days))).isoformat(), "nonce": secrets.token_hex(16)}
    return pol


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True)
    ap.add_argument("--installation-id")
    ap.add_argument("--environment", choices=("production", "preview"))
    ap.add_argument("--demo-only", action="store_true")
    ap.add_argument("--max-accounts-per-user", type=int, default=1)
    ap.add_argument("--allowed-brokers", default="*")
    ap.add_argument("--max-risk-pct", type=float)
    ap.add_argument("--max-daily-loss-pct", type=float)
    ap.add_argument("--expires-days", type=int)
    ap.add_argument("--reason", required=True)
    ap.add_argument("--issuer")
    ap.add_argument("--migrate-from", help="old signed policy_migration json to take installation id / environment / issuer from")
    ap.add_argument("--out", required=True)
    ap.add_argument("--no-sign", action="store_true", help="write the unsigned body (CI signs it)")
    a = ap.parse_args()
    if a.migrate_from:
        old = json.load(open(a.migrate_from, encoding="utf-8"))
        mig = old.get("policy_migration") or old
        a.installation_id = a.installation_id or mig.get("installation_id")
        a.environment = a.environment or mig.get("environment")
        a.issuer = a.issuer or mig.get("issuer")
        a.demo_only = a.demo_only or bool(mig.get("demo_only"))
    if not (a.installation_id and a.environment and a.issuer):
        raise SystemExit("--installation-id, --environment and --issuer are required (or --migrate-from)")
    if a.expires_days is None:
        a.expires_days = 30 if a.demo_only else 45
    pol = build(a)
    import rules_policy as rp
    if not a.no_sign:
        from release_signing import sign_hex
        pol["signature_hex"] = sign_hex(rp.body(pol), purpose=rp.PURPOSE)
        errs = [e for e in rp.problems(pol) if "installation_id" not in e and "environment" not in e]
        if errs:
            raise SystemExit("policy invalid: " + "; ".join(errs))
    os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(pol, fh, indent=2, sort_keys=True)
        fh.write("\n")
    print(f"rules policy {a.version} ({'DEMO-only' if a.demo_only else 'LIVE allowed'}, cap {a.max_accounts_per_user}/user, "
          f"brokers {pol['allowed_brokers']}, {a.expires_days} days) → {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
