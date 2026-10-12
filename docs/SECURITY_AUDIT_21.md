# Security Audit #21 — main128 (main127 review fixes + Repair cleanup v1.10 + close-only banner)

Scope: every surface changed in main128 — read-only `inventory_projection.projection` + `auto_approve_under_rules`
write step and its four call sites; `GET /accounts/close-only-notice`; admin-first `PATCH /accounts/{id}`; server
auto-adoption after the identity chain (`server_adopt.py`, bridge heartbeat, `_token_kind`, manual lock, flip-flop
finding `server_flipflop`); `/bot/start` scope-then-gate and `/bot/stop` stop-all; `rules_policy.problems`
fail-closed without `STOIC_INSTALLATION_ID`, `effective_max_risk_pct` / `risk_caps`, identity-bound operator
approval; runtime risk clamps (`bot_runner`, `circuit_breakers`); orphan quarantine (`bot_configs_orphaned`);
auto-attest re-check/void; installer v1.10 `Remove-StoicStaleEaCharts`; `deploy/update.sh` `|| true` +
`release_precheck.release_run` event widening; `CloseOnlyBanner.jsx`. Plus the standard perimeter sweep.
Documented false positives (PRD "IGNORE IT") and the audit #20 P3 notes were not re-reported.

**Verdict: PASS — no P0/P1/P2. Three P3 hardening notes; two fixed in this release.**

| ID | Sev | Location | Finding | Status |
|---|---|---|---|---|
| SEC-021-1 | P3 | `routes/account_routes.py` `PATCH /accounts/{id}` | Admin edits of another user's `label`/`group`/`trading_enabled` were audited only on the server-name branch, and admin-first used a bare `role == "admin"` check rather than the MFA-enrolled `require_admin` gate. No escalation for non-admins. | **FIXED** — cross-owner edits call `require_admin(user)` (TOTP-enrolled) before any change and write `account_edited_by_admin` with `owner_user_id`, `on_behalf`, per-field `from/to`. Test `test_sec021_admin_cross_owner_edits_are_mfa_gated_and_audited`. |
| SEC-021-2 | P3 | `routes/account_routes.py` `close_only_notice` | Banner ran a platform-wide `projection(db, None)` per call and filtered to the caller afterwards — no leak (only `per_user_blocks[caller]` returned) but write-free load amplification. | **FIXED** — `projection(db, user["id"])`: caller-scoped, nothing foreign computed. |
| SEC-021-3 | P3 | `auto_attest.py` | A heartbeat with the account's own valid bridge token reporting real/contest mode voids even a manual DEMO attestation. Restrictive direction only (account moves toward LIVE/close-only), audited, requires the account's token. | Accepted — by design (broker truth wins, U9); monitored via `account_environment_attestation_voided`. |

## Confirmed safe
- `auto_approve_under_rules` writes only when `auto_approval_due` (hash changed AND no blocking platform violation) →
  no audit-chain spam from the EA-token heartbeat path; `/authority/inventory` scope is admin→platform / user→own.
- Server adoption: only after `verify_heartbeat_identity`, FULL token, active installation + lease bound to the
  account, 24 h manual lock; grace/revoked tokens cannot adopt; `SERVER_NAME_RE` bounds the reported name; the
  flip-flop finding is deduped by `open_or_update` key.
- `/bot/start` and `/bot/stop`: owner-scoped (`user_id`), CSRF via global middleware, step-up on live.
- Rules policy: Ed25519 verification path unchanged; production fails closed without the installation id; risk caps
  can only tighten and every exception path keeps the stricter user limit.
- Quarantine copies trading knobs only (no credentials); auto-attest void is fail-closed.
- Installer v1.10: local-only, scoped `Get-ChildItem` under the resolved data folder, backups, encoding handled.
- `update.sh` `|| true` touches only the advisory precheck — `verify_attestation` / `verify_release_provenance`
  still gate after checkout; a `workflow_dispatch` run needs repo write and never satisfies the signature gate.
- Perimeter: CORS allowlist, cookie flags, CSRF + production origin check, rate limits on
  register/login/2FA/reset/refresh/step-up/pairing, admin + step-up on every inventory write.

## Not exercised
Read-only review plus live preview curl of `close-only-notice`; `CloseOnlyBanner.jsx` renders server strings as
React text nodes (no `dangerouslySetInnerHTML`).
