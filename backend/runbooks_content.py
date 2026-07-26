"""STOIC operational runbooks (iter-136) — served by GET /api/admin/runbooks."""

INCIDENT_RESPONSE_MD = """# Incident Response Playbook

**Owner:** Platform admin · **Review cadence:** quarterly · **Version:** 2026-06

## Severity levels
- **SEV-1** — live trading placing wrong/unauthorized orders, account takeover, payment forgery, data breach.
- **SEV-2** — trading halted platform-wide, EA bridge outage, payment processing down.
- **SEV-3** — degraded component (email, analytics, one broker), no capital at risk.

## SEV-1: Immediate containment (first 15 minutes)
1. **Stop all trading:** Admin → `POST /api/admin/panic` (global panic) — disables every bot and closes managed positions. Verify on /status and the admin dashboard.
2. **Freeze VPS agents:** if an agent is suspect, freeze its command queue (Infrastructure → agent → freeze) — frozen agents only accept diagnostics.
3. **Revoke execution leases:** compromised account → revoke its execution lease so no writer can trade it.
4. **Lock accounts:** suspend the affected user(s) in Admin → User Management (mid-session block is immediate).
5. **Rotate the compromised secret** (see Secret Rotation below).

## Secret rotation map
- **User bridge token** → Accounts page → ROTATE TOKEN (15-min grace for the old token; update the EA input within that window).
- **Ed25519 release key** → generate a new key, set `ED25519_SIGNING_KEY_B64` in the deployment env, redeploy; agents pin the new key from `GET /api/release-key`.
- **JWT secret** → change `JWT_SECRET` env + redeploy; all sessions are invalidated (users re-login).
- **Stripe key** → roll in the Stripe dashboard, update deployment env var, redeploy.
- **Resend key** → roll in Resend dashboard, update env, redeploy.
- **Admin password** → Admin → User Management → force password change; admin MFA remains enrolled.

## Evidence preservation
- The admin audit log is hash-chained. Run `GET /api/admin/audit/verify` and save the output BEFORE and AFTER the incident window.
- Export relevant collections (trades, audit logs, sessions) before any cleanup.
- Do not delete suspect accounts — suspend them (deletion destroys evidence).

## Communication
1. Update the public /status page reality: components degrade automatically, but post a support broadcast if user action is needed.
2. Reply to affected users through the in-app Support queue (email notifications go out automatically).
3. For payment incidents, reconcile against the Stripe dashboard before communicating amounts.

## Post-incident (within 72h)
- Timeline: what happened, when detected, when contained.
- Root cause + the guardrail that should have caught it.
- Actions: new tests (CI manifest), new alerts, key rotations completed.
- Verify audit chain integrity again and archive the report.
"""

BACKUP_RESTORE_MD = """# Backup & Restore Runbook

**Owner:** Platform admin · **Version:** 2026-06

## What must never be lost
- `users`, `subscriptions`, `payment_transactions` (billing truth — Stripe holds the money-side ledger as an independent copy)
- `mt5_accounts` / `accounts`, `bot_configs`, `trades`
- `admin_audit_log` (hash-chained — partial loss is detectable via /api/admin/audit/verify)

## Platform responsibilities (managed by Emergent)
The production database and container images are operated by the Emergent platform. Verify with platform support:
1. **Daily automated MongoDB backups** — confirm schedule, retention window, and storage location.
2. **Point-in-time recovery** availability and granularity.
3. **Container image scanning** — base images are maintained by the platform build pipeline.
4. **TLS everywhere** — ingress TLS is platform-managed; the EA bridge authenticates with per-account tokens over HTTPS (mTLS is not applicable to MT5 terminals).

## Application-level export (belt and braces)
From a machine with DB access:
```
mongodump --uri "$MONGO_URL" --db "$DB_NAME" --gzip --archive=stoic-$(date +%F).gz
```
Store encrypted, off-platform, with 30-day retention minimum.

## Restore drill (run quarterly)
1. Restore the latest dump into a THROWAWAY database name:
   `mongorestore --uri "$MONGO_URL" --nsFrom "$DB_NAME.*" --nsTo "drill_$DB_NAME.*" --gzip --archive=<file>`
2. Point a local backend at `DB_NAME=drill_...` and verify:
   - admin login works, user counts match expectations
   - `GET /api/admin/audit/verify` → chain OK
   - a sample user's trades/subscription state look correct
3. Record drill date + duration + any gaps in this runbook's changelog.
4. Drop the drill database.

## Recovery priorities (RTO order)
1. Database up + backend healthy (`/health`) — everything else depends on it.
2. Disable all bots until data is verified (`POST /api/admin/panic` if in doubt) — a bot trading on stale/rolled-back data is worse than downtime.
3. EA bridge reconnects automatically once the API is reachable; positions are reconciled by heartbeat deep-sync.
4. Payments: verify recent Stripe sessions against `payment_transactions`; Stripe is authoritative for money movement.

## Security checklist ownership
| Item | Owner | Where |
|---|---|---|
| MFA for administrators | App (enforced) | require_admin gate |
| Signed releases | App (Ed25519) | /api/release-key |
| mTLS host agents | N/A for MT5 — token auth over HTTPS | bridge tokens |
| Short-lived tokens | App | rotating sessions, pairing codes, token grace |
| Secret rotation | Admin + this runbook | rotation map |
| Immutable audit logs | App (hash chain) | /api/admin/audit/verify |
| Daily backups | Platform — verify with support | above |
| Restore drills | Admin (quarterly) | this runbook |
| Dependency scanning | CI + periodic pip-audit/yarn audit | dev workflow |
| Container scanning | Platform | build pipeline |
| Penetration testing | External vendor (recommended annually) | — |
| Incident response | Admin | playbook above |
"""


def get_runbooks() -> dict:
    return {"runbooks": [
        {"id": "incident-response", "title": "Incident Response Playbook",
         "markdown": INCIDENT_RESPONSE_MD},
        {"id": "backup-restore", "title": "Backup & Restore Runbook",
         "markdown": BACKUP_RESTORE_MD},
    ]}
