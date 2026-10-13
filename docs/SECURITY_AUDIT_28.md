# Security Audit #28 — Verified Performance archive, Infrastructure backups/clock, v1.60.28 execution fixes, secret hygiene, deployed-app perimeter

Scope: every surface changed since audit #27 (`docs/SECURITY_AUDIT_27.md`): Verified / Public Performance with the new
deleted-account "archived" rows (`routes/performance_routes.py`, `account_tombstones.py`,
`scripts/backfill_account_tombstones.py`, `components/VerifiedPerf.jsx`, both performance pages); Infrastructure
shadow-certification clock detail and the admin server-backups listing (`routes/infra_routes.py`, docker-compose `:ro`
mount); the v1.60.28 execution fixes (`close_commands.cancel_pending` / `settle_cancelled_trades`,
`execution_intents.settle_intent_from_trade` / `reconcile_terminal_trades` / `sweep_intents`,
`background_loops._execution_intents_sweep_loop`, `workers/reconciliation.py`, `portfolio/var.py` stop-bounded VaR,
`risk.min_lot_fits_budget` / `bot_runner._floor_or_skip` / `execution_authority` REDUCE floor); the secret-scanner
`concatenated-credential-literal` rule and the patched `backend/tests/test_audit_p3_live_iter228.py`; plus a READ-ONLY
unauthenticated perimeter sweep of the DEPLOYED app (https://stoicaibot.com) compared with the preview. Source traced
end-to-end; no mutating request, no credential used, no secret printed or committed.

**Verdict: PASS — no P0/P1/P2. Two P3 hardening notes.**

| ID | Sev | Location | Finding | Status |
|---|---|---|---|---|
| SEC-028-1 | P3 | git history, commit `3465fd97` (`backend/tests/test_audit_p3_live_iter228.py`) | The admin password was committed as a string-concatenated literal (scanner evasion). The current tree reads `TEST_ADMIN_PASSWORD` from env and `scripts/secret_scan.py` now blocks the concatenated shape; the historical commit still carries the value. | OPEN — OPERATOR ACTION: rotate the production admin password + revoke sessions, update `backend/.env` / `memory/test_credentials.md`, append the OLD value's sha256 to `scripts/retired_credentials.sha256`. History rewrite optional. |
| SEC-028-2 | P3 | preview `/api/*` | Preview API answers `Access-Control-Allow-Origin: *` (no credential reflection; auth is cookie + CSRF so no private data is readable cross-origin). Production reflects nothing for a foreign Origin. | ACCEPTED — non-production surface. |
| SEC-027-1 / SEC-027-2 | P3 | `GET /api/infra/artifacts/manifest`, `GET /api/status` | Carried, unchanged, by design. | ACCEPTED |

## Confirmed safe
- Archived rows (`performance_routes.py` `_verified_payload`): `archived_per` keys come only from the caller's own
  `broker_deals` (user-scoped query); tombstone / soft-deleted-account lookups are by globally unique account `_id`, so a
  user can never surface another user's tombstone label or broker. No cross-tenant leak.
- Public share (`public_performance`): when the attestation gate fails every headline/financial field is stripped
  (including the previously un-popped `net_pnl`) and archived rows drop `account_id_suffix` / `deleted_at` / `broker`;
  masked labels are `ACCOUNT-n` / `ARCHIVED-n`; bogus / revoked share ids return the generic 404 with `no-store`
  (probed live).
- XSS: `VerifiedPerf.jsx`, `VerifiedPerformance.jsx`, `PublicPerformance.jsx` render user-authored labels only as JSX
  text; no `dangerouslySetInnerHTML`.
- `server_backups` (`infra_routes.py`): admin-only via `_admin_ok` (role + enrolled 2FA); lists only `*.archive.gz`
  names / sizes / mtimes from `STOIC_BACKUP_DIR` (operator env, no request input, no path traversal, never file
  contents). Clock detail exposes only the account label and the broker offset.
- Order cancellation (`close_commands.cancel_pending` / `settle_cancelled_trades`): every caller pins `user_id`
  (`risk_manager` `{"_id": oid, "user_id": user_id}`, `nl_routes` via `target_filter` / `trade_query`). No cross-tenant
  cancel.
- Execution intents: transitions allow-listed; `settle_intent_from_trade` / `reconcile_terminal_trades` /
  `sweep_intents` apply only legal terminal moves and leave terminal intents untouched; internal callers only — no
  user-supplied `intent_id` privilege path. The sweep loop runs in the reconciliation worker with a `loop_progress`
  heartbeat.
- Sizing floor (`execution_authority.py` REDUCE): `signal["_sizing"]` is produced only server-side by `bot_runner`;
  the manual `execute_signal` path never passes it, so `min_lot_fits_budget({})` fails closed (skip, not floor). Not
  attacker-influenced.
- `portfolio/var.py`: `PORTFOLIO_VAR_STOP_GAP_FACTOR` is operator env only; `_stop_bounded_loss_usd` guards
  `entry > 0`, `sl <= 0` / `sl == entry` / wrong-side stops → `None` (parametric figure stands); the bound only ever
  LOWERS the caller's own self-risk figure and can never reach 0 for a priced position.
- `scripts/backfill_account_tombstones.py`: insert-only (never overwrites), orphan-only targets, prints ids / counts /
  environment, no secrets.
- Perimeter (prod https://stoicaibot.com): HSTS preload, CSP with `frame-ancestors 'none'`, X-Frame-Options DENY,
  nosniff, Referrer-Policy, COOP / CORP, Permissions-Policy present; `/docs`, `/openapi.json`, `/redoc`, `/metrics`,
  `/.env`, `/.git/config` all serve the SPA `index.html`; login is Turnstile-gated (403 before any credential check),
  no `Set-Cookie` on failure; foreign-origin requests get no ACAO.

## Coverage and limits
- Reviewed: performance verified / public / share + attestation gate, account_tombstones + backfill script,
  infra_routes backups / clock / overview, close_commands and its callers, execution_intents + sweeps + worker wiring,
  var.py, execution_authority REDUCE floor + bot_runner `_floor_or_skip`, secret_scan rule + patched test + git history
  (commit confirmed, value not reproduced), frontend sinks, unauthenticated prod + preview sweep.
- No authenticated / mutating session was exercised (source + read-only probes), consistent with prior baselines;
  cookie flags taken from source + audit #26.
