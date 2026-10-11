# Security Audit #20 — re-verification of audit #19 fixes + code added since (StatusFixDialog, WebRequestGuide `force`)

Scope: (a) RE-VERIFY the two audit #19 fixes — SEC-001 (pairing token in the `.cmd` download URL) and SEC-002
(no cap on pairing-token issuance); (b) fresh review of everything added after #19 — `StatusFixDialog.jsx`
(install/repair one-liner, trust-terminal, adopt-reported-server, WebRequest guide, info), `WebRequestGuide.jsx`
`force` prop, `QuickInstallPanel.jsx` (`setupCmdUrl(cmdPath)`), test changes (`test_fixplan_main113.py`,
`test_phase2_installer.py`), re-frozen `release/rc_lock.json`; (c) perimeter regression sweep — CORS /
`CHECKOUT_ALLOWED_ORIGINS`, cookie flags, CSRF double-submit on cookie-auth mutations, login/OTP/step-up failure
limits, `require_admin` coverage, secrets in repo / `.gitleaksignore` scope. Method: source review; no production
access-log inspection (pre-fix pairing tokens had a 60-min TTL and are all expired). Documented false positives
(`trade_eval` ≠ `eval`, Terms.jsx DOMPurify, lazy-import cycles, localStorage banner flags) not re-reported.

**Verdict: PASS — no material functional issue; audit #19 SEC-001 and SEC-002 confirmed fixed.**

## Re-verification of audit #19

| ID | Status | Evidence |
|---|---|---|
| SEC-001 | **FIXED (re-verified)** | `routes/setup_routes.py` — `pairing-token` returns `cmd_download_path=/api/setup/installer.cmd?d=<handle>`; handle = `secrets.token_urlsafe(18)` (~144 bit, random → no timing oracle). `GET /setup/installer.cmd` requires `get_current_user`; handle regex `^[A-Za-z0-9_-]{16,64}$` → 400; unknown AND not-yours both → identical 404 (no existence/ownership enumeration gap); 410 only after ownership passes; token/login/repair baked into the FILE BODY only; `Cache-Control: no-store` + `Referrer-Policy: no-referrer`. Repo grep: no `installer.cmd?token=` / `token=` in any `href`, `window.open` or `location`; both frontends consume `cmd_download_path` only. |
| SEC-002 | **FIXED (re-verified)** | `rate_limit(db, "pairing_token_issue", f"user:{user['id']}", 10, 900)` runs BEFORE any DB read/write in `issue_pairing_token`; identifier is the authenticated user id; atomic `$inc` fixed window → 429. The volume-bypass header is non-production only and constant-time compared. |

## Fresh scope — confirmed safe
- `StatusFixDialog.jsx` / `QuickInstallPanel.jsx` / `WebRequestGuide.jsx`: no `dangerouslySetInnerHTML`; every server
  string rendered as escaped text; clipboard + `href` values are server-derived only (`install_command`,
  `cmd_download_path`, `webrequest_url`); POSTs (`trust-terminal`, `adopt-reported-server`, `pairing-token`) go
  through the axios client that adds `X-CSRF-Token`; both mutations are `require_step_up` + audited server-side.
- `status-line`, `adopt-reported-server`, `trust-terminal`: `{_id, user_id}`-scoped (BOLA OK); `status_line.derive`
  operates on the owner's account only.
- `connect_service.install_command`: login digits-only, token urlsafe-only, base URL server-derived.
- Tests: fixtures hold placeholder credentials only; `rc_lock.json` re-frozen via `scripts/freeze_rc_lock.py`.

## Perimeter sweep — confirmed safe
CORS credentialed only for the filtered allowlist (wildcard never with credentials); cookies `HttpOnly; Secure;
SameSite=None`; CSRF double-submit on every cookie-auth mutation; login / email-OTP / step-up failure limits in
place; `require_admin` (role + TOTP) on all admin gates; no `.env` or secret committed; `.gitleaksignore` limited to
pinned fixture/history fingerprints.

## Hardening notes (P3, no action required)
- `routes/account_routes.py` `install-progress`: `role == "admin"` may read any account's progress — intended
  support path; keep in the least-privilege review list.
- `InfraWizard.jsx` infra-agent bootstrap one-liner displays its bootstrap token inside a URL string (separate
  flow from MT5 pairing; the token is shown to the operator, not navigated to). Backlog: consider the same
  body-only handle pattern as SEC-001.
