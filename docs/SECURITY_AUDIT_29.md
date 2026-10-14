# Security Audit #29 — v1.60.31 release content (14 Oct fix list A1–E10), deployed-app perimeter

Scope: every surface changed since audit #28 (`docs/SECURITY_AUDIT_28.md`), i.e. the `## v1.60.31` section of
`docs/CHANGELOG.md`: Scalp Fast Path redesign + live gate (`scalp/setup.py`, `scalp/engine.py`, `scalp/live_gate.py`,
`scalp/instruments.py`, `routes/scalp_routes.py`); DEFERRED/HOLD decision ledger + scoreboard funnel
(`routes/trade_routes.py`); certification polling (`routes/account_routes.py`, `AccountCertification.jsx`,
`pages/Accounts.jsx`); Easy Start config / `PUT /bot/config` default modes + one-click drift fix (`routes/bot_routes.py`,
`EasyStartWizard.jsx`); `safety_status` attested-environment labels + `paper_accounts`; backup matcher accepting
`.archive.gz.enc` (`security_agent/checks/integrity_platform_trading.py`, `routes/infra_routes.py`); runtime watchdog
1 s tick (`runtime_watchdog.py`); TIPS FRED `DFII10` fallback + backoff (`macro/tips.py`); `scripts/freeze_rc_lock.py`
image digests; docker-compose `HF_HUB_DISABLE_XET=1`; the new HTTP smoke test `tests/test_v16029_smoke_http.py`; a
regression re-check of the #28 execution items (`workers/reconciliation.py`, `execution_intents.py`, `portfolio/risk.py`);
plus a READ-ONLY unauthenticated perimeter sweep of the DEPLOYED app (https://stoicaibot.com) compared with the preview.
Source traced end-to-end; no mutating request, no credential used, no secret printed or committed.

**Verdict: PASS — no P0/P1/P2. Two carried P3 items, two P3 hardening notes.** _Update 2026-10-14: SEC-029-1 CLOSED — production password rotated by the operator, preview re-hashed, sessions revoked, old fingerprint retired in `scripts/retired_credentials.sha256`._

| ID | Sev | Location | Finding | Status |
|---|---|---|---|---|
| SEC-029-1 | P3 | git history, commit `3465fd97` (`backend/tests/test_audit_p3_live_iter228.py`) | Carried from SEC-028-1: the production admin password is still recoverable from history. Current tree reads `TEST_ADMIN_PASSWORD` from env; the new smoke test (`tests/test_v16029_smoke_http.py:12-13`) also reads credentials only from env / `.env` — no literal. | OPEN — OPERATOR ACTION: rotate the production admin password + revoke sessions, update `backend/.env` / `memory/test_credentials.md`, append the OLD value's sha256 to `scripts/retired_credentials.sha256`. |
| SEC-029-2 | P3 | preview `/api/*` | Carried from SEC-028-2: preview answers `Access-Control-Allow-Origin: *`, `Allow-Headers: *`, no `Allow-Credentials`. Auth is cookie + CSRF so no private data is readable cross-origin. Production reflects nothing for a foreign Origin. | ACCEPTED — non-production surface. |
| SEC-029-3 | P3 | `GET /api/health`, `GET /api/status` | build_sha, release tag, component versions and the `env_sig` HMAC output (a hash, not a key) are public — carried by design from SEC-027-1/2. | ACCEPTED |
| SEC-029-4 | P3 | production edge | HSTS is `max-age=31536000; includeSubDomains` without `preload`. Add the token only if the domain is submitted to the preload list. | NOTE |

## Confirmed safe
- **A1/A2 scalp live gate**: `scalp/engine.py` `apply_config` forces `mode="shadow"` server-side whenever
  `live_gate.live_eligibility` is not eligible; `live_traded` is only set when `self.mode == "demo_live"`. XAUUSD stays
  shadow until EURUSD passes the gate (`live_gate.py`). `PUT /scalp/config` (`routes/scalp_routes.py`) allow-lists
  symbols / modes, requires `confirm_live`, and binds the account to `user["id"]`. `/scalp/status` filters runners by
  `user_id` — `requested_mode` / `live_gate` never expose another user's data. No user input reaches sizing or execution.
- **B3 scoreboard funnel** (`routes/trade_routes.py`): trades and the `trade_decisions` aggregation both match on
  `user_id`; bounded (`to_list(3000)`, `days` window); `last_reason` is engine / defer text with no secrets.
- **B4 certification** (`routes/account_routes.py`): user-scoped query, EA version from `ea_version` /
  `ea_identity.ea_version`; read-only and cheap enough for the 30 s poll + focus refresh.
- **C5 Easy Start / `PUT /bot/config`** (`routes/bot_routes.py`): Pydantic model (no mass assignment); live activation and
  any risk raise still require `require_step_up` + audit and go through `_refuse_risk_above_policy_cap`; the one-click
  drift fix only sends `{active: true}` through that same gated path. `_get_or_create_config` default modes per
  environment confirmed. Nothing in the fix list changes risk settings or trading on/off silently.
- **C6 `safety_status`**: user-scoped; `attested_environment` drives the LIVE count; `paper_accounts` is a count only.
- **D7 backup matcher**: operates on `os.listdir` names from the operator-set `STOIC_BACKUP_DIR`; suffix allow-list
  (`.archive.gz`, `.archive.gz.enc`), no request input, no traversal / wildcard, never file contents or secrets.
- **D8 watchdog**: 1 s async heartbeat tick, bounded work, internal only — no DoS surface.
- **D9 TIPS** (`macro/tips.py`): fixed Treasury / FRED URLs built from an integer year only (SSRF-free), 12 s timeout,
  keyless, negative-cache backoff; trusted-source XML parsed to floats — no cache-poisoning input path.
- **E10 `scripts/freeze_rc_lock.py`**: binds commit, backend / frontend image digests, tree SHAs and evidence sha256;
  `authoritative` requires commit + target; CI-only inputs.
- **docker-compose `HF_HUB_DISABLE_XET=1`**: benign download-path toggle.
- **#28 re-check**: `workers/reconciliation.py`, `execution_intents.py`, `portfolio/risk.py` — no regression in the
  changed wiring; sweeps remain internal-only, cancel paths user-pinned, sizing floor server-side.

## Perimeter (read-only, unauthenticated)
- Production https://stoicaibot.com: HTTP → HTTPS 308; HSTS / CSP (`frame-ancestors 'none'`) / `X-Frame-Options: DENY` /
  `nosniff` / `Referrer-Policy` present; `/api/docs`, `/api/openapi.json`, `/api/redoc` → 404; `/.env`, `/.git/HEAD`
  serve the SPA index (no file disclosure); foreign `Origin` receives no `Access-Control-Allow-Origin`.
- Preview: same route posture; wildcard CORS as noted in SEC-029-2.

## Gaps
- No authenticated or mutating session exercised (read-only policy); cookie flags taken from source + prior baselines.
