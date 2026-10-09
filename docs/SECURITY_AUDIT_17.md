# Security Audit #17 — 2026-10 (scope: changes since audit #16 — main117 review / audit A20 fixes + black-box preview)

Scope: auth.py `stoic_session` hint cookie + frontend refresh-skip / 8 s auth deadline (api.js, AuthContext.jsx);
ops/production_reconcile.py `--expect approved` (signed-policy counts + ids) + deploy/update.sh wiring;
host_profile.py 12 h warning + host_profile_alerts.py + ops_routes severity; deploy/preflight.sh
`host_prereqs_missing` timer check + strict `write_host_profile_file`; deploy/host-prereqs.sh (d) timer units
(6 h + jitter, --check evaluation); host-profile-refresh.sh; doctor.sh timer/age checks; STOIC-Agent.ps1 1.4
(`Set-StoicCrashDumpPolicy`, `Test-StoicLedgerReadBack`); scripts/test_agent_recovery.ps1 + ci.yml 5.1 step.
Black-box (unauthenticated, non-destructive) on the preview deployment; regression spot-check of core controls.

Verdict: **CONDITIONAL PASS (no P0–P2) → P3 hardenings applied in this commit.**

| ID | Sev | Location | Finding | Fix |
|----|-----|----------|---------|-----|
| H-1 | P3 (theoretical) | `backend/ops/production_reconcile.py` (`--expect approved`) | An approved policy with an EMPTY `account_ids` list silently degraded the gate to counts-only (exact-id checks skipped). Unreachable in production (`validate_expectation` requires ids) but a silent downgrade. | REFUSED (exit 2) when the approved policy names no account ids while `enabled > 0`. |
| H-2 | P3 (theoretical) | `backend/ops/production_reconcile.py` (`enabled_environments_all_demo`) | The DEMO-only reconcile check classified accounts by the user-declared `broker_environment()` — the same declared-vs-attested gap as SEC-001 (#16). Impact limited to release evidence (the trading gate already uses `attested_environment`). | Rows carry `attested_environment`; totals add `attested_environments_enabled`; the demo_only check uses the attested set. |
| H-3 | P3 (info) | `backend/auth.py` `stoic_session` | Constant `"1"`, JS-readable, `SameSite=None; Secure`, 30 d — reveals only "a session may exist" to same-origin scripts; mirrors the existing JS-readable CSRF cookie. | Accepted (no secret; needed cross-site because the API origin differs from the SPA origin on the preview). |

Verified-OK controls (no change): hint-cookie refresh-skip cannot bypass auth (anonymous 401 ⇒ logged-out, never
user set); the 8 s `Promise.race` deadline leaves no dangling state write; `host_profile` failure paths stay
fail-closed (`ops_routes` exception ⇒ `ok = not is_production()`); deploy env knobs (`STOIC_HOST_PROFILE_DIR`,
`STOIC_SYSTEMD_DIR`) consistently quoted, no attacker-controlled interpolation; systemd units root-only
(accepted #16); agent 1.4 writes HKCU only, no secret handling regression; CI Windows step uses a static path, no
untrusted `${{ }}` in `run:`. Black-box preview: `/api/auth/me` + `/api/auth/refresh` anonymous ⇒ 401 (never 500);
`/api/ops/*` ⇒ 403/404; hostile `Origin` not reflected, no `Access-Control-Allow-Credentials`; CSP
`default-src 'none'`, HSTS, XFO DENY, nosniff. Regression spot-check: JWT algorithm pin, admin TOTP gate,
`bridge_token_hash`, Stripe webhook verification, release-key pin (no TOFU), SHA-pinned actions — unchanged.

Gaps: no authenticated black-box session (policy); timer/writer not verified on the real cPanel host; agent not run
on a real Windows PowerShell 5.1 box (CI step only); symlink/TOCTOU on `deploy/state` relies on the root-owned repo
directory (not demonstrated).
