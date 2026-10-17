# Security Audit #31 — audit-follow-up release content + in-app Status page

Scope: every surface changed since audit #30: the Status page now rendered inside `AppLayout` for signed-in users
(`frontend/src/pages/StatusPage.jsx`, `routes/portal_routes.py /status`); policy disclosure (`policy_summary.py`,
`/api/bot/status`, `/api/health`, `/api/state/readiness`); release-lock handling (`release_gate.py` `RC_LOCK_PATH`,
`lock_matches_build()`, `deploy/update.sh`, compose read-only lock mount); alert-email verification + Telegram
settings doc (`notification_settings.py`, `routes/notification_routes.py`, `notifier.py`); the global 422 handler
(`server.py`); scalp reports (`scalp/reports.py`, `routes/scalp_routes.py`); engine persistence / scalp exit rules
(`execution.py`, `trade_manager.py`); plus the standing auth/JWT/CORS/security-header/secret-handling review and a
READ-ONLY inspection of the preview deployment (`/api/status` body + response headers). No mutating request, no
secret printed or committed.

## Verdict

**PASS — NO MAJOR ISSUE FOUND.** No path for an outsider or an ordinary user to read another user's data, bypass
login, forge trades, or recover secrets. Confidence HIGH (full read-only source access + live inspection).

## Verified (by focus area)

| Area | Result |
|---|---|
| Public `/status` + in-app StatusPage | Signed-in and anonymous views call the identical `/api/status`; only the layout wrapper differs. Live body: component health, 12-char inventory hash, input version, short commit, decision id, `app_env` — no account ids, e-mails, tokens or raw counts. Fails closed on timeout. |
| `policy_summary.py` / status disclosure | Policy version, demo-only flag, expiry, label only. |
| `release_gate.py` RC_LOCK_PATH / `lock_matches_build()` | `RC_LOCK_PATH` is operator env, never HTTP input → no traversal. Missing/corrupt lock → `{}`; `evaluate()` and `lock_matches_build()` fail CLOSED. Compose mounts the lock read-only. |
| Alert e-mail verification | Code stored as sha256(user:addr:code), 10-min TTL, 5-attempt cap, `hmac.compare_digest`; start endpoints rate-limited 3/900 s (≈15 guesses per 15 min vs 10⁶ space). |
| Telegram / alert settings doc | Every read/write scoped by `user_id` (no IDOR). Bot token vault-encrypted; only `has_token` returned. `alert_test` registered in `STEP_UP_ACTIONS` and enforced; destinations must be verified. |
| 422 handler (`server.py`) | Returns `loc/msg/type` only — request body / password never echoed. |
| Scalp reports | All endpoints require `get_current_user`, filter by `user["id"]`; queries bounded (`to_list(5000)`, `limit` clamped). Engine filter uses the `SCALP_ENGINES` constant. |
| `execution.py` engine / `trade_manager.py` | `engine`/`scope` derived server-side from `engine_for_signal(signal)`; scalp `mode` allow-listed. |
| Auth / JWT / CORS / headers | HS256 with explicit algorithm allow-list, secret required from env, session-id revocation + `tokens_valid_after`, HttpOnly+Secure+SameSite cookies with CSRF token. Credentials-CORS only with the production allowlist. Live headers: HSTS, CSP, `X-Frame-Options: DENY`, nosniff, COOP/CORP. `.env` files git-ignored and untracked. |

## Hardening (P3, optional)

1. `routes/scalp_routes.py` `/scalp/broker-stats?broker=` returns aggregate per-broker execution stats not scoped to
   the caller (non-PII); consider scoping to brokers the user has an account with.
2. `routes/notification_routes.py` alert-email verify-confirm has no route-level throttle (relies on the per-challenge
   5-attempt cap + rate-limited start). Adequate; an explicit confirm limiter adds defense in depth.
3. Dependency advisory scan (pip/yarn) was not executed in this read-only pass — covered by the CI requirements guard.

## Coverage and limits

Checks applied: BOLA/BFLA, authentication bypass, weak recovery/verification, CSRF/CORS, path traversal, injection
(engine/scope), information disclosure, step-up enforcement, security misconfiguration, resource bounding.
Deployment inspected is `app_env=preview`; production-only CORS/MFA branches verified by source, not at runtime.
