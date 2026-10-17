# Security Audit #22 — main129–main131 (Easy Start Wizard, Add-ons, main130/131 review fixes)

Scope: every surface changed since audit #21 (`docs/SECURITY_AUDIT_21.md`) — Easy Start Wizard
(`routes/wizard_routes.py` owner-only read + `use-reported-login` cross-user 409 and trust reset,
`EasyStartWizard.jsx`), rules-policy `load_latest` (GitHub fetch, per-file `problems()` wrap), `retire_id_list`,
renewal, `.github/workflows/rules-policy.yml`, Go-Live checklist + `release_signed_verdict`; Add-ons
(`prediction_track.py` + `GET /trades/predictions`, `chart_patterns.py` + `gate_decision`, bot_runner Spec B block +
`raise_alert` dedup, posture `chart_patterns`, `pattern_gate_enabled` admin gate, C1–C3); main131
(`account_deletion.py`, no-force `DELETE /accounts/{id}`, `POST /admin/accounts/{id}/force-delete`,
`release_truth.release_id` on public `/api/health`, `SiteFooter.jsx`, `TrackRecord.jsx` provenance,
`.github/workflows/model-manifest-resign.yml`, `scripts/secret_scan.py` rule change, `open_findings.json` →
`closed_findings.json` consolidation). Plus the standard perimeter sweep and read-only public curls against preview
and production (no production login, no mutating request anywhere). Documented false positives (PRD "IGNORE IT")
and audit #21 P3 notes were not re-reported.

**Verdict: PASS — no P0/P1/P2. Four P3 hardening notes; two fixed in this release, two accepted by design.**

| ID | Sev | Location | Finding | Status |
|---|---|---|---|---|
| SEC-022-1 | P3 | `server.py` `/api/health` · `release_truth.release_id` | Public health probe now also carries tag, EA version, rules-policy version and model-manifest version/sha-prefix (build_sha was already public) — incremental reconnaissance value only. | ACCEPTED BY DESIGN — main131 P2-04 explicitly requires one release ID (tag + commit + EA + policy + model manifest) on `/api/health` and the UI footer; none of the fields is a secret and all are verifiable artifacts. Re-evaluate if a non-authenticated footer is ever removed. |
| SEC-022-2 | P3 | `routes/bot_routes.py` `update_config` | `pattern_gate_enabled` was guarded by the bare role string, not the TOTP-enrolled `require_admin` gate (toggle is restrictive-only and audited). | FIXED — `require_admin(user)` added (403 `admin_mfa_required` for un-enrolled admins in production). |
| SEC-022-3 | P3 | `routes/trade_routes.py` `trade_predictions` | No per-user rate limit although the handler upserts outcome rows (bounded: scan 500 / read 5000). | FIXED — `rate_limit("trade_predictions", user, 60/60 s)`. |
| SEC-022-4 | P3 | `scripts/secret_scan.py` admin/ops-literal rule | Quoted value must now be whitespace-free (prose false-positive fix). A passphrase containing spaces in that one form would no longer match this heuristic; other rules (login pair, prose pair, retired hashes) unchanged. | ACCEPTED — real tokens are whitespace-free; `--history` 0 blocking; regression tests `test_secret_scan_prose_fp.py`. |

## Confirmed safe
- Account deletion: normal `DELETE` is owner-scoped (`{_id, user_id}`) and refuses on open/pending/UNKNOWN trades or
  stale broker truth (`account_deletion.delete_refusal`); force-delete is `require_admin` + `require_step_up`
  (bypass header honoured only when `not is_production()`, boot guard refuses bypass tokens in production),
  reconciles the account's own owner, records `owner_user_id` in the audit chain, error bodies carry only that
  account's counts — no IDOR, no cross-owner purge path.
- Wizard: both routes load `{_id, user_id}`; `use-reported-login` adopts only the EA-reported login, 409 on own
  duplicate and on a cross-user login with a heartbeat < 24 h BEFORE any write, clears attestation/admission, audited;
  CSRF via the global middleware; poll rate-limited.
- Add-ons: `/trades/predictions` symbol allowlist regex → 422, user-scoped queries, bounded scans; `gate_decision`
  observe-only by default, fail-closed only when an admin enabled it; alert dedup per account+symbol.
- Rules policy: GitHub repo from operator env (never user input), downloads restricted to `raw.githubusercontent.com`,
  every candidate passes Ed25519 + installation id + environment + expiry + nonce; malformed files rejected per file;
  all policy writes admin + step-up.
- Workflows: inputs only through env vars (no `${{ }}` shell interpolation), allowlist-validated `policy_version` /
  `installation_id`, external signer, reviewer environment, plain push (no force).
- Findings register: every removed entry is in `closed_findings.json` as `superseded` → a still-open item; the only P0
  (`P0-01-r17-infra`) remains open; `check_open_findings.py` OK.
- Perimeter: unauthenticated `/setup/wizard/*`, `/trades/predictions`, `/admin/accounts/*/force-delete` → 401;
  `secret_scan.py --history` 0 blocking; CORS allowlist + cookie flags + CSRF unchanged.

## Not exercised
Authenticated flows were verified from source plus preview HTTP tests (`test_reports/iteration_251.json`), not by the
auditor's own session. No mutating request was sent to production (which still runs a build without `release_id`).
