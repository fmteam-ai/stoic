# Security Audit #16 — 2026-06 (scope: changes since audit #15 + black-box on the deployed preview)

Scope: `git diff b67d97e6..HEAD` — host_profile.py (daily-refreshed signed `/app/state/host_profile.json`, staleness /
future-dated / unparseable ⇒ UNVERIFIED), trading_authority.py (degraded VPS agent policy + recovery), ops_routes.py,
STOIC-Agent.ps1 (manual MT5 login default for real money, WER dumps off, per-loop ledger re-probe),
deploy/host-prereqs.sh + host-profile-refresh.sh (systemd timer) + preflight.sh, docker-compose.yml `/app/state:ro`,
check_deploy_python_snippets.py, AddAccountWizard.jsx, release/rc_lock.json + ledger-anchors.jsonl.
Black-box (unauthenticated, non-destructive) on the preview deployment; regression sweep of core controls.

Verdict: **CONDITIONAL PASS → fixed in this commit.**

| ID | Sev | Location | Finding | Fix |
|----|-----|----------|---------|-----|
| SEC-001 | P2 (MEDIUM, likely) | `backend/trading_authority.py:96-98` | Degraded-agent branch classified the account with `broker_environment()` (user-declared `broker_environment` / `account_type` / server name) instead of the server-authoritative `attested_environment()` used by every other money gate. A real-money account *declared* as demo (EA not reporting a real trade mode) kept `REDUCED` (new half-size exposure) instead of `CLOSE_ONLY` while automatic MT5 restarts were suspended — contradicting A19-P1-03. | `attested_environment(account)`: only PAPER or an admin attestation (identity-bound, voided by a broker-reported real trade mode) keeps `REDUCED`; everything else `CLOSE_ONLY`. Tests: `test_main116_a19.py::test_degraded_agent_close_only_for_real_reduced_for_demo` (declared-demo ⇒ CLOSE_ONLY, attested-demo ⇒ REDUCED, broker-real voids ⇒ CLOSE_ONLY), `test_main115_a18.py::test_infrastructure_domain_flags_degraded_agent` updated. |
| H-1 | P3 | `deploy/preflight.sh:216` | Signed `host_profile.json` written `0644` on a shared host. | Accepted — public signed artifact (profile + markers, no secrets); the HMAC key never leaves root-owned storage. |
| H-2 | P3 (info) | `backend/static/STOIC-Agent.ps1:308-311` | WER LocalDumps suppression is HKCU-scoped; a machine-wide LocalDumps policy could still capture agent dumps. | Accepted — documented residual (needs admin on the VPS; in-memory exposure already documented in the script). |

Verified-OK controls (no change): host-profile file — atomic `mv`, root-only timer unit, derived HMAC key, >24 h / >5 min
future / unparseable ⇒ UNVERIFIED, env fallback stays stale ⇒ UNVERIFIED; `deploy_jam` marker fail-closed at the choke
point; agent recovery not trivially resettable (durable read-back write + 10-min window, re-probed per loop); no
injection / unquoted expansion in the new shell scripts; `/app/state` mounted `:ro`. Black-box preview: CSP
`default-src 'none'`, HSTS, XFO DENY, nosniff; `/api/ops/*` anonymous ⇒ 403/404 (never 500); no credentialed CORS
reflection (wildcard only on public `/api/status`, `/api/health`); session cookies HttpOnly + Secure + SameSite=None
(CSRF cookie intentionally JS-readable). Regression: JWT algorithm pinned; `require_admin` TOTP gate; `bridge_token_hash`
HMAC-SHA256; Stripe webhook signature-verified; release-key pin refuses TOFU and sidecar fallback; workflows env-bind
untrusted context and SHA-pin external actions.

Gaps: no authenticated black-box session (policy); host-profile file ownership not checked on the real cPanel host;
unchanged payments / legacy BOLA routes not re-reviewed; forging the profile requires the root-held `ledger_anchor_key`
(documented, accepted).
