# Security Audit #19 — M122 + "STOIC Simple Onboarding Spec" Phases 1–4 (EA v1.64, installer v1.9)

Scope: everything added since audit #18 — `status_line.py` + `GET /api/accounts/{id}/status-line`,
`adopt-reported-server` / `trust-terminal` / server PATCH, `server_adopt.py`, `auto_attest.py`,
`account_tombstones.py` (410), `bot_inventory.py` (one bot per account), `rules_policy.py` +
`scripts/sign_rules_policy.py` (Ed25519 signed rules policy, demo auto-admission), `setup_routes.py`
(`pairing-token` repair flag, `installer.cmd` launcher, `install-progress`, `pairing-status`),
`STOIC-Installer.ps1` v1.9 (`-Login` binding, `-Repair`) + `connect_service.install_command`,
`POST /admin/deploy/latest` (GitHub `workflow_dispatch`), security-agent Cloudflare preflight bypass + `no_data`
findings → `info`, EA drop-file readers (`StripBom`, shape validation), frontend `StatusFixDialog` / `StatusLine` /
`AddAccountWizard` (server optional) / `WebRequestGuide` / `EditAccountDialog` / `RulesPolicyPanel`.
Method: source review against "IDOR on account-scoped endpoints, step-up/CSRF coverage, credential leakage via
URLs, command injection into the generated PowerShell/.cmd line, SSRF/open-redirect in deploy dispatch, signature
bypass in rules-policy loading, rate limits, information disclosure in status internals, tombstone enumeration,
demo-attestation abuse, secrets in repo, untrusted HTML rendering". Known false positives from earlier rounds
(`trade_eval` ≠ `eval`, Terms.jsx DOMPurify, lazy-import cycles, localStorage banner flags) were not re-reported.

**Verdict: CONDITIONAL PASS → both items fixed the same day (this release).**

## Summary

| ID | Sev | Location | Finding | Fix |
|---|---|---|---|---|
| SEC-001 | P2 | `routes/setup_routes.py` `GET /setup/installer.cmd`, `QuickInstallPanel.jsx`, `StatusFixDialog.jsx` | The ".cmd download" convenience link carried the single-use pairing token in the URL query string (`?token=…&login=…&repair=`) on an unauthenticated GET. URLs are recorded by Cloudflare/proxy access logs and browser history; a log reader could redeem the token via `POST /setup/claim-pairing` within its 60-min TTL and hijack that one account's pairing (bridge token for the account). Bounded: single account, single-use, owner-rotatable, no trade execution without being the terminal. | FIXED: `pairing-token` now also issues an opaque 10-min **download handle** (`cmd_download_path = /api/setup/installer.cmd?d=<handle>`, stored on the `pairing_tokens` doc with `download_handle_expires_at`, `repair`, `mt5_login`). `installer.cmd` requires the **owner's session cookie** (`get_current_user` + `user_id` match), resolves token/login/repair server-side and bakes them into the FILE BODY only; responds `Cache-Control: no-store` + `Referrer-Policy: no-referrer`; 400 malformed / 404 unknown-or-not-yours / 410 expired. A logged URL now yields nothing redeemable (handle is useless without the victim's session and dies in 10 min). Both frontends consume `cmd_download_path`; the token never appears in any URL. Test `test_u7_double_clickable_launcher_served_per_pairing` (owner OK, repair OK, headers, 400/404/404-stranger/410). |
| SEC-002 | P3 | `routes/setup_routes.py` `POST /setup/pairing-token` | No per-user cap on token issuance (auth'd + ownership-checked; each issue only replaced the prior token — hygiene). | FIXED: `rate_limit("pairing_token_issue", user, 10 / 15 min)` → 429. Verified live (10 × 200 then 429). |

## Re-verified (confirmed safe)
- **Ownership (BOLA/BFLA)**: `status-line`, `adopt-reported-server`, `trust-terminal`, account PATCH (server edit),
  `install-progress`, `pairing-status`, `pairing-token` all resolve the account under `{_id, user_id: user.id}`.
- **Step-up MFA + audit**: server adopt / edit / trust-terminal require `require_step_up`; `admin/deploy/latest`
  requires `require_admin` (TOTP) + step-up `authority_relax`; CSRF double-submit covers every cookie-auth mutation.
- **Rules policy**: Ed25519 only, pinned release public key(s) with purpose-domain separation (`RELEASE_PURPOSES`),
  no algorithm/key-id confusion, `expires_at` enforced, fails closed (no policy → no auto-admission).
- **Demo attestation**: `broker_reports_real` always wins over any demo claim; a DEMO attestation needs an
  installer-attested, release-signed EX5 hash on an authoritative identity chain (`auto_attest.py`, `broker_env.py`).
- **Command injection**: `connect_service.install_command` gates `login` to digits and the token to
  `[A-Za-z0-9_-]`; `base_url` is server-derived (never trusts `Host` blindly); `.cmd` escaping is `"` → `\"` only
  on a server-built string.
- **Deploy button**: fixed `api.github.com` host, tag taken from GitHub `releases/latest`, token from env
  (`GITHUB_DISPATCH_TOKEN`), 503 `deploy_button_not_configured` when absent — no SSRF / open redirect.
- **Tombstones**: store hashes only; 410 carries no account data.
- **Status-line internals**: server name / position mode / EA version / admission state of the OWN account only.
- **Frontend**: no `dangerouslySetInnerHTML` in any new component.
- **Secrets**: none in repo; test fixtures use low-entropy runtime generators; `.gitleaksignore` scoped to fixtures.

## Not exercised
Source-only review plus live curl verification of SEC-001/SEC-002 on the preview backend. Production access logs
were not inspected; rotating any pairing issued before this fix is unnecessary (all have expired — 60-min TTL).
