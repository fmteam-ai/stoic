# Turnstile — state machine, configuration and runbook (authoritative)

Implementation: `backend/turnstile_gate.py`, `backend/turnstile_break_glass.py`,
`backend/degraded_login_otp.py`, frontend `src/components/TurnstileWidget.jsx`.
Tests: `tests/test_iter236_turnstile_round9.py`, `tests/test_iter170_prod_boot.py`,
`tests/test_iter155_cloudflare_edge.py`. This document, the code and the tests
describe ONE policy: **fail-closed**. There is no fail-open path anywhere.

## 1. Public state — `GET /api/auth/turnstile-config`

| `state` | `code` | Meaning | Frontend behaviour |
|---|---|---|---|
| `disabled` | `policy_disabled` | Admin policy off (`platform_state.turnstile.enabled=false`) | submit allowed, no widget |
| `ready` | `ready` | Policy on, both keys present, provider healthy | widget mounts; **submit blocked until a token exists** |
| `misconfigured` | `keys_incomplete` | Policy on but `TURNSTILE_SITE_KEY`/`TURNSTILE_SECRET_KEY` incomplete | submit **blocked**, operator-alert copy; production refuses to boot (`production_config_violation`) and `release-readiness.turnstile_config` fails |
| `provider_degraded` | `provider_unavailable` | Cloudflare siteverify failed within the last 60 s | widget still mounts; login may submit without a token **only** when `degraded_login=otp_required` — server then requires a bound emailed code |
| `break_glass` | `break_glass_active` | Governed incident bypass active | surfaces in `scope` submit without a widget; others behave as `ready` |

Response never contains the secret. `degraded_login` echoes
`TURNSTILE_LOGIN_DEGRADED_POLICY` (`closed` default, `otp_required`).

## 2. Verification outcome (per request, `verify_token` + `bind_claims`)

| Outcome | Cause | HTTP to client |
|---|---|---|
| `ok` | `success=true` AND action exactly equals the surface AND (allowlist empty OR hostname non-empty ∧ allowlisted) AND `challenge_ts` within `TURNSTILE_MAX_TOKEN_AGE_SECONDS` (300) AND token not seen before (`turnstile_consumed_tokens`, sha256 only) | pass |
| `client_token_invalid` | missing/invalid/duplicate token, `action-missing`, `action-mismatch`, `hostname-missing`, `hostname-mismatch`, `token-stale`, `token-replayed` | `403 turnstile_required` (retryable) |
| `provider_unavailable` | transport error / 5xx / `internal-error` | `503 turnstile_unavailable` — except login with `otp_required` (→ §3) |
| `configuration_invalid` | `invalid-input-secret`, `missing-input-secret`, `bad-request`, unknown codes | `503 turnstile_unavailable` — always closed, logged at ERROR |

## 3. Gate decision (`evaluate()` → `GateDecision.mode`)

```
force_disabled     TURNSTILE_FORCE_DISABLE=true, non-production ONLY (refused + audited in production)
policy_disabled    admin policy off
break_glass        governed record active AND action ∈ scope → durable bypass event (no token stored)
deny               configuration_invalid | client_token_invalid | provider_unavailable (closed policy)
degraded_otp_required  provider_unavailable ∧ action=login ∧ policy=otp_required
verified           ok
```

`require_turnstile()` (register, password_reset) raises on `deny` **and** on
`degraded_otp_required` — those surfaces never degrade. `/auth/login` calls
`evaluate()`; on `degraded_otp_required` it first verifies the password
(wrong password ⇒ generic `401 Invalid email or password`, no code issued), then
`degraded_login_otp.challenge()` issues a 6-digit code bound to user + salted
IP/UA context + reason, TTL 5 min, 5 attempts, single-use, 30 s resend cooldown,
6 codes / 15 min. Client codes: `turnstile_degraded_otp_sent`,
`turnstile_degraded_otp_invalid`, `turnstile_degraded_otp_expired`. TOTP-enrolled
users satisfy the second factor with TOTP instead. When the provider recovers
the next login verifies normally with no operator action.

## 4. Configuration

| Variable | Required | Notes |
|---|---|---|
| `TURNSTILE_SITE_KEY` / `TURNSTILE_SECRET_KEY` | when policy enabled | same widget; production boot fails if policy on and either missing |
| `TURNSTILE_EXPECTED_HOSTNAMES` | production | comma list; when set, hostname claim is mandatory |
| `TURNSTILE_MAX_TOKEN_AGE_SECONDS` | no | default 300, min 30 |
| `TURNSTILE_LOGIN_DEGRADED_POLICY` | no | `closed` (default) or `otp_required` |
| `TURNSTILE_FORCE_DISABLE` | never in production | dev convenience only; refused + audited in production |
| ~~`TURNSTILE_BREAK_GLASS_UNTIL/REASON`~~ | retired | replaced by governed DB record (§5) |

Admin policy toggle: `POST /api/admin/settings/turnstile {enabled}` (refuses to
enable without both keys). Diagnostics: `GET /api/ops/turnstile-diag` (admin).

## 5. Break-glass (incident procedure)

Activate: `POST /api/admin/settings/turnstile/break-glass`
`{incident_id, approver (≠ actor), reason (≥20 chars), scope=["login"], ttl_minutes ≤ 60}`.
`register`/`password_reset` scope additionally requires `allow_registration_reset: true`.
Effects: record persisted in `platform_state.turnstile_break_glass`; activation,
deactivation and review appended to the hash-chained `admin_audit_log`; critical
`ops_alerts` row pages operators; every bypassed request → `turnstile_bypass_events`
(incident, action, salted IP hash, request id — never the token); `bypass_count`.
Expiry is automatic at `until`. Deactivate early:
`POST …/break-glass/deactivate`. Post-incident review (required, refused while
active): `POST …/break-glass/review {note ≥20 chars}`.
**Promotion is blocked** (`release-readiness.turnstile_break_glass`) while a
record is active OR unreviewed.

## 6. Runbook

1. Users report `turnstile_required` in bulk → `GET /ops/turnstile-diag`: check
   `recent_rejections[].error_codes` — `hostname-mismatch`/`action-mismatch`
   mean widget/env drift (fix `TURNSTILE_EXPECTED_HOSTNAMES` or the widget
   hostnames); `INVALID_SECRET` means the key pair is from different widgets.
2. `503 turnstile_unavailable` with `state=provider_unavailable` → Cloudflare
   outage. Decide: wait, set `TURNSTILE_LOGIN_DEGRADED_POLICY=otp_required`
   (login only, needs Resend), or open a governed break-glass (§5).
3. `state=configuration_invalid` → our fault; never bypass — fix the keys.
4. After any break-glass: deactivate (or let expire), complete the review, confirm
   `release-readiness` is green before promoting.
