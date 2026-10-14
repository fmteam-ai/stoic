# Security Audit #30 — v1.60.32 release content, deployed-app perimeter

Scope: every surface changed since audit #29 (`## v1.60.32` in `docs/CHANGELOG.md`): step-up actions `alert_test` /
`host_migration` (`step_up.py`, `routes/auth_routes.py`); the dedicated per-user notification settings doc, legacy
migration, Telegram webhook-secret lookup, alert e-mail verification and per-category routing
(`notification_settings.py`, `routes/notification_routes.py`, `routes/telegram_routes.py`, `notifier.py`,
`routes/diagnostic_routes.py`); the global 422 handler (`server.py`); adopted-trade alerts
(`routes/bridge_routes.py notify_adopted_open`); scalp breakeven / time-stop exits (`trade_manager.py`); keyed FRED +
TIPS single-flight (`macro/fred.py`, `macro/tips.py`, `integrations_settings.py`); scalp reports (`scalp/reports.py`,
`routes/scalp_routes.py`); engine persistence + loss-lab backfills (`strategy_engines.py`, `loss_postmortem.py`,
`trade_decisions.py`, `trading_readiness.py`); `scripts/secret_scan.py` retired-literal handling; five changed
frontend components; plus a READ-ONLY unauthenticated perimeter sweep of production (https://stoicaibot.com,
running v1.60.31) and the preview (v1.60.32). Source traced end-to-end; no mutating request, no credential used, no
secret printed or committed.

**Verdict: PASS — no P0/P1/P2. One P3 live finding (fixed in v1.60.32, awaiting deploy), two carried P3 notes.**

| ID | Sev | Location | Finding | Status |
|---|---|---|---|---|
| SEC-030-1 | P3 | production `POST /api/auth/login` (any 422) | Production (tag v1.60.31) still echoes the submitted request body in the FastAPI default 422 `input` — a mistyped password bounces back to the sender. Self-reflection only; no cross-user exposure. The fix `server.py _validation_error_without_echo` (loc / msg / type only) is in v1.60.32 and verified on the preview (canary absent). | OPEN until v1.60.32 is deployed — re-run the canary POST afterwards. |
| SEC-030-2 | P3 | preview `/api/*` | Carried SEC-029-2: wildcard CORS without credentials on the non-production surface; production reflects nothing for a foreign Origin. | ACCEPTED |
| SEC-030-3 | P3 | `GET /api/health`, `GET /api/status` | Carried SEC-029-3: build sha / release tag / `env_sig` hash public by design. | ACCEPTED |

## Confirmed safe
- **Step-up**: `alert_test` / `host_migration` registered (`step_up.py`); `/auth/step-up` and `require_step_up` still
  require TOTP / passkey enrollment plus a fresh single-use token; `step_up_verified` / `step_up_failed` audited. No
  action became issuable without MFA.
- **Notification settings** (`notification_settings.py`): every read/write is `(user_id, kind)`-scoped; the legacy
  migration copies then `$unset`s only within the caller's rows; `by_webhook_secret` matches a 32-char random secret and
  the webhook handler still verifies `chat.id`; the sealed Telegram token is never serialized (`has_token` bool only).
  Alert e-mail: code hash `sha256(user:addr:code)` compared with `compare_digest`, 5 attempts / 10 min, start
  rate-limited 3 per 15 min, endpoints self-scoped (no enumeration); an UNVERIFIED alert address never receives mail
  (`alert_email_for`); security mail (login OTP, password reset, new-login) never routes through `alert_email_for`;
  `valid_email` rejects whitespace / newlines (no header injection).
- **422 handler**: drops `input`, keeps `loc / msg / type` — the frontend reads `msg`, nothing breaks.
- **Adopted-trade alerts** (`bridge_routes.py notify_adopted_open`): `notified_opened` idempotency flag; `user_id` comes
  from the authenticated account; no cross-user notification.
- **Scalp exits** (`trade_manager.py`): MODIFY_SL / FULL_CLOSE go through `_free_slot_filter` + `request_close` (EA
  command authorization intact); breakeven only tightens (`already` check, entry + costs on the profitable side);
  `SCALP_BE_TRIGGER_R` / `SCALP_TIME_STOP_MIN` parsed from env only.
- **FRED / TIPS**: `FredError` text never carries the keyed URL; fixed hosts, 10–12 s timeouts; key never logged;
  per-loop `asyncio.Lock` single-flight; `FRED_API_KEY` sealed, validator `^[a-f0-9]{32}$`, `test_provider("fred")`
  prints only the FredError text or exception type.
- **Scalp reports**: every query `user_id`-scoped; `days` clamped 1–365; symbols from `APPROVED` only; `to_list(5000)`.
- **Backfills**: `ensure_pattern_keys_canonical` `$regex` is a constant; `backfill_trade_engines` matches a constant
  engine list; `trade_decisions` TTL on `expires_at`; `trading_readiness` lists only the caller's accounts.
- **Secret scanner**: a concatenated literal is downgraded only when its sha256 is in the reviewed retired list; any
  other literal still blocks; `scripts/retired_credentials.sha256` holds hashes + provenance comments only.
- **Frontend** (`ScalpReports.jsx`, `AlertTestPanel.jsx`, `Notifications.jsx`, `TradingReadinessStrip.jsx`,
  `AdminIntegrations.jsx`): no `dangerouslySetInnerHTML`; no secret values rendered (write-only fields, `configured` /
  `has_token` bools); `delivered_to` arrives masked from the server.

## Perimeter (read-only, unauthenticated)
- Production https://stoicaibot.com: HTTP → HTTPS 308; HSTS (now `includeSubDomains; preload`), CSP, `X-Frame-Options:
  DENY`, `nosniff`, Referrer-Policy present; `/api/docs`, `/api/openapi.json` → 404; `/.env`, `/.git/HEAD` serve the
  SPA index; foreign `Origin` gets no `Access-Control-Allow-Origin`. **422 canary echoed** (SEC-030-1, v1.60.31).
- Preview: same route posture; wildcard CORS (SEC-030-2); **422 canary NOT echoed** (v1.60.32).

## Gaps
- No authenticated or mutating session exercised (read-only policy); cookie flags taken from source + prior baselines.
