# Security Audit #25 — v1.60.25 → v1.60.28 (EA-count gate, certification badge + fix hints, rules-policy attestation)

Scope: every surface changed since audit #24 (`docs/SECURITY_AUDIT_24.md`): v1.60.25 `ops/prepromotion_evidence.py`
(`worker_enabled_view` / `ea_count_gate`); v1.60.26 `routes/account_routes.py accounts_certification` (now reading
`position_mode_resolution` / `broker_offset_sec`), `routes/bridge_routes.py` heartbeat one-shot deep-sync bootstrap;
v1.60.27 `_certification_fix()` guidance + `AccountCertification.jsx` actions (DOWNLOAD EA / REQUEST SYNC / CERTIFY NOW);
v1.60.28 `routes/portal_routes.py _compute_status` attestation declared by an active signed rules policy, 16-wide
bounded per-account READY check; standard perimeter sweep (CORS, cookies, CSRF, `require_admin` TOTP, headers, generic
errors, `secret_scan.py`). Read-only: source traced end-to-end plus unauthenticated probes against the preview; no
mutating request, no credential printed or committed.

**Verdict: PASS — no P0/P1/P2. Two P3 notes; one fixed in this release, one accepted by design.**

| ID | Sev | Location | Finding | Status |
|---|---|---|---|---|
| SEC-025-1 | P3 | `routes/account_routes.py` `POST /accounts/{id}/request-sync` | Owner-scoped and idempotent on one field, but no throttle: an owner could repeatedly re-queue a 7-day history re-read on their own terminal (self-inflicted, bounded to their own EA; not a platform DoS). | FIXED — `rate_limit(db, "request_sync", user_id, 6, 3600)` as on `trust_terminal` / `revoke_installation`; while a sync is already pending the route returns `already_pending: true` without re-queuing. Regression test `test_request_sync_is_rate_limited_and_idempotent`. |
| SEC-025-2 | P3 | `routes/portal_routes.py` public `/status` | Exposes a 12-char inventory digest and the monotonic authority `input_version`. | ACCEPTED — non-sensitive (truncated hash + version counter), no identifiers or raw counts; unchanged prior design (r15 P1-02). |

## Confirmed safe
- EA-count gate (v1.60.25): both sides count ENABLED accounts only (`worker_enabled_view` filters `account_enabled`,
  `ea_count_gate` compares to `trading_enabled` rows); a disabled account with a live EA can neither satisfy nor fail
  it, any enabled/worker disagreement fails closed, and the bundle still records `eas_connected_all_accounts`.
  Offline operator CLI, not attacker-reachable.
- History-sync bootstrap (v1.60.26): exactly one request per account (`history_sync_bootstrap_at` set once in the same
  update, never cleared), only for identity-verified terminals, only on heartbeats authenticated by the account's bridge
  token (`_account_by_token`). No repeat / amplification path; `broker_offset_sec` and `position_mode_resolution` are
  pure functions over the account document.
- Certification badge + fix hints (v1.60.27): `accounts_certification` queries `{"user_id": user["id"]}` only;
  `_certification_fix` texts embed EA version strings only — never tokens or secrets. `certify` and `request-sync`
  resolve the account scoped to the caller (no IDOR); `certify` re-verifies every check server-side. The frontend renders
  `label` / `value` / `fix.text` as React text nodes — no `dangerouslySetInnerHTML`, no HTML sink; DOWNLOAD EA points
  at the public `GET /api/ea-script`.
- Public status (v1.60.28): attestation needs a declared expectation (fixed counts OR an ACTIVE, unexpired signed rules
  policy), zero platform violations / structural defects, `approved_hash == inventory_hash`, every enabled account
  READY and a stable authority version; the gather runs inside the try/except that forces `attested=False` on any
  exception (fail-closed), is 16-wide with short-circuit, under the 20 s budget and single-flight cache. Live probe:
  `attested:false`, no account identifiers, no raw counts.
- Perimeter: `require_admin` TOTP gate and step-up on token / type / server / trust edits unchanged; CORS credentialed
  only for the allowlist; access/refresh cookies HttpOnly + Secure + SameSite, CSRF double-submit covers the touched
  POSTs; security headers and generic 401 bodies present; `python3 scripts/secret_scan.py` → 0 blocking (194 informational, rotated).

## Coverage and limits
- No auditor-held authenticated session was exercised (source + public read-only probes), consistent with the #24 baseline.
- NoSQL / SSRF / path / template injection: not applicable — no new reachable sink in scope.
