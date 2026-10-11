# policies/ — signed RULES policies (Phase 3, schema `stoic.policy-rules/v1`)

One signed file = the admission rules for an installation: `demo_only`, `max_accounts_per_user`,
`allowed_brokers` (`"*"` = any broker in the registry), `risk_caps`, `expires_at` (≤ 45 days demo-only,
≤ 180 days otherwise). Release key v2 signature (purpose `policy-rules`), bound to `installation_id` and
`environment`, single-use `nonce`.

**No release needed.** The server activates the newest valid file here at startup (`rules_policy.load_from_repo`)
or an admin uploads one in Admin → Policy (`POST /api/authority/policy/upload`, step-up). It only verifies.

- Demo accounts that satisfy the rules are **admitted automatically** (audit `account_admission_admitted`).
- Real-money accounts wait for **one operator approval** in-app (`POST /api/authority/admission/{id}/approve`,
  password + authenticator) — never under a `demo_only` policy.
- The inventory configuration event is recorded automatically once every enabled account is admitted (U14).

Create / migrate: `python scripts/sign_rules_policy.py --migrate-from release/policy_migrations/<old>.json
--version demo-rules-v1 --reason "…" --out policies/demo-rules-v1.json` (release signer token required).

O4 — the GitHub environment used by `policy-migration.yml` must have a **required reviewer** configured in
repository Settings → Environments; a rules policy for real money must not be signed by the person who
proposes it.
