# Security Audit #24 — v1.60.24 disabled-account scoping + v1.60.23 re-check

Scope: every surface changed since audit #23 (`docs/SECURITY_AUDIT_23.md`): v1.60.24 user-level decision scoping
(`canonical_decision._decide_user_uncached` / `account_has_exposure`, `state_contract.contract` / `quick_truth`,
`routes/portal_routes.py /status` attestation, `routes/account_routes.py close_only_notice`, `AuthorityStrip.jsx`,
`CloseOnlyBanner.jsx`); v1.60.23 re-verification (StepUpDialog as Radix dialog, `PATCH /accounts/{id}` `account_type`
step-up `account_type_edit` + 409 `demo_only_policy` / `not_admitted`, `inventory_projection.per_account_blocks`,
`auto_attest.declare_demo`); standard perimeter sweep (CORS, cookies, CSRF, `require_admin` TOTP, security headers,
generic error bodies, `secret_scan.py`). Read-only: source traced end-to-end plus unauthenticated GET probes against the
preview; no mutating request, no credential printed or committed.

**Verdict: PASS — no P0/P1/P2. Two P3 hardening notes; one fixed in this release, one accepted by design.**

| ID | Sev | Location | Finding | Status |
|---|---|---|---|---|
| SEC-024-1 | P3 | `state_contract.py` `quick_truth` | The user-level `position_truth` now ignores an account switched OFF without exposure, so `panic_available` (which keyed on that aggregate) could read false while an OFF, disconnected account still had an UNKNOWN broker count. | FIXED — `panic_available` is additionally true whenever ANY account row (OFF included) reports non-FRESH truth; regression test `test_quick_truth_panic_stays_available_for_off_account_with_unknown_broker_truth`. |
| SEC-024-2 | P3 | `canonical_decision.py` `_decide_user_uncached` | Disabling an account removes its blockers from the USER-level strip (state OFF, counted=False). Latent safety / position-truth issues on a no-exposure OFF account are no longer surfaced in the ribbon. | ACCEPTED — requested behaviour (prod report: "ignore disabled accounts there"); execution is gated per account (below); OFF accounts stay enumerated in the drill-down and every admin/operator view (`/authority/decision/{account_id}`, inventory projection, Accounts page). |

## Confirmed safe
- A switched-off account cannot open new exposure regardless of the user-level state: every choke point
  (`execution.py` `enforce_new_trade` / `gate_or_block`, `execution_authority.py`, `meta_decision.py`,
  `binance_engine.py`) evaluates the PER-ACCOUNT decision, and `trading_authority.account_domain` returns LOCKED whenever
  `trading_enabled is not True`. `nl_execution` / `trigger_sweeper` use the user decision only to suppress risk-increasing
  actions and re-check fresh authority before every effect.
- Exposure is never invisible: `account_has_exposure` keeps an OFF account counted while it has local open trades or a
  broker-reported `open_positions` > 0 — only the `ACCOUNT_DISABLED` blocker is dropped, stale/unknown truth still blocks.
- `/status` (public): attestation requires `platform_violations` + `structural_defects` clear AND an approved hash AND
  every ENABLED account READY via `decide_account` (per-account blocks still force CLOSE_ONLY there); live probe shows
  `attested:false`, no account identifiers or raw counts; r15 P1-02 / r16 P1-06 invariants hold (overall degrades when
  trading is not ready).
- `close_only_notice`: `projection(db, user["id"])` is caller-scoped and the account loop is `{"user_id": user["id"]}` —
  only the caller's own labels/reasons are emitted (no IDOR, no cross-tenant text); protected endpoints answer 401
  unauthenticated.
- Frontend: labels / reasons render as React text nodes (`AuthorityStrip.jsx`, `CloseOnlyBanner.jsx`); no
  `dangerouslySetInnerHTML`, no HTML sink.
- v1.60.23 gates intact: `require_step_up(db, user, request, "account_type_edit")` precedes any write, audit
  `account_type_edited` (`step_up=True`), 409 `demo_only_policy` under a demo-only policy, 409 `not_admitted` on the API
  path when enabling an unadmitted account; step-up tokens are single-use, TTL- and action-bound, TOTP/passkey gated;
  `auto_attest` grants DEMO only on broker truth from a signed + verified EA and voids on a real-money report.
- Perimeter: `require_admin` enforces TOTP enrolment in production; CORS credentialed only for the allowlist (wildcard
  without credentials otherwise); httpOnly/Secure/SameSite cookies + CSRF double-submit unchanged; security headers and
  generic JSON error bodies unchanged; `python3 scripts/secret_scan.py` → 0 blocking (194 informational, rotated).

## Coverage and limits
- No auditor-held authenticated session was exercised (source + public read-only probes), consistent with the #23 baseline.
