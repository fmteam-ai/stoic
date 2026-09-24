# Release Acceptance Checklist (audit round 3 — AT-01 … AT-16)

Status legend: **AUTOMATED** = runs in this repo's suites · **DEPLOYED-ONLY** = needs the
staging/production stack, broker demo accounts or EAs — cannot be claimed from source · **PARTIAL**.

| AT | Scope | Status | Evidence / where to run |
|----|-------|--------|-------------------------|
| AT-01 | six-account execution boundary (3 true / 2 false / 1 missing) | AUTOMATED + STAGING DRILL | `backend/ops/at01_account_boundary.py` seeds 6 real-shaped rows and asserts state contract, worker selection, authority domain, execution choke point (`account_enablement_lock` → `account_not_enabled`) and broker-request stub agree on the same 3 ids, zero intents for false/missing. Runs in CI (`test_at01_drill_passes_against_this_stack`) and on staging via `make staging-acceptance` (evidence `release/drills/at01-*.log`) |
| AT-02 | enablement boolean on every creation/import/migration path | PARTIAL | creation paths write explicit `trading_enabled: False` (iter-201); DB schema validator for accounts is **not yet applied** — run `deploy/mongo-validators.js` once written (P1 follow-up) |
| AT-03 | authority fault matrix (8 domains × FULL/REDUCED/CLOSE_ONLY/timeout/exception/malformed) | AUTOMATED (unit) | `tests/unit/test_authority_matrix.py`; shared snapshot id API↔execution **DEPLOYED-ONLY** |
| AT-04 | UNKNOWN reconciliation | PARTIAL | `tests/test_iter193_audit_p0.py`, soak invariants; ambiguous broker submission needs the demo broker → **DEPLOYED-ONLY** |
| AT-05 | position truth / EA freshness transitions | PARTIAL | `tests/test_iter200_release_review.py`; cross-surface EA total reconciliation → **DEPLOYED-ONLY** |
| AT-06 | read-only health + fail-closed caps | AUTOMATED | `tests/test_iter204_audit_acceptance.py::test_at06_*`, `tests/test_iter201_release_review.py` (static write-free GET, `health_truth_unavailable` cap 25). Live Mongo write-capture (profiler) → run `db.setProfilingLevel(2)` during GET storm on staging |
| AT-07 | atomic repair ledger (fail before / after mutation / during finalization, replay exactly once, idempotent sweeps, before/after preserved) | AUTOMATED | `tests/test_iter204_audit_acceptance.py::test_at07_*` |
| AT-08 | immutable audit controls | PARTIAL | app layer AUTOMATED (`test_at08_*`: hash chain tamper/removal detection, update/delete refusal + critical alert, read-only routes, `/admin/repair-ledger/verify`). DB-credential-level denial, signed anchors, SIEM export → **DEPLOYED-ONLY** (restricted Mongo role for the app user is in `deploy/mongo-init.js` scope, not yet split per collection) |
| AT-09 | live promotion soak | AUTOMATED (unit) | `soak_campaign` invariants (`UNKNOWN_RATE_MAX_LIVE=0.0`, `unknown_rate=None` ≠ evidence); full 14-day campaign → **DEPLOYED-ONLY** |
| AT-10 | performance attestation populations | AUTOMATED (unit) | `tests/test_iter200_release_review.py`, performance share gate tests |
| AT-11 | public metric integrity | AUTOMATED | `test_at11_*`: deleted/disabled/paper/demo/QA/duplicate fixtures, HOLD ≠ blocked, availability withheld until reconciled 30-day edge window; independent probe logs → `/api/public/edge-probe` (prober must run **outside** the platform) |
| AT-12 | forecast behaviour states | AUTOMATED (unit) | `tests/test_iter201_release_review.py` (fail-open labelling, `record_decision`), `scripts/verify_forecast_profile.py` on the forecast profile |
| AT-13 | authenticated route + security matrix | PARTIAL | HTTP suites cover anonymous/user/cross-tenant/admin on admin routes; axe/keyboard/chunk-failure → testing-agent Playwright runs (iteration_181/182 reports); full matrix → **DEPLOYED-ONLY** |
| AT-14 | Turnstile/Cloudflare | PARTIAL | `tests/test_iter15x_turnstile*.py` (success/expiry/rejection/offline fail-closed); replay/slow-challenge at the edge → **DEPLOYED-ONLY** |
| AT-15 | signed release + rollback | AUTOMATED (static) + STAGING DRILL | static: `test_at15_*`, `tests/unit/test_release_attestation.py`; drill: `make staging-acceptance ROLLBACK=1` → `deploy/drills/at15_rollback_drill.sh` verifies attestation + image signatures/digests for the running commit, forces readiness to fail (`STOIC_DRILL_FORCE_READINESS_FAIL=1`, fail-closed hook), runs `deploy/update.sh`, asserts auto-rollback restored HEAD / GIT_SHA / signed digest / schema / container set and READY (evidence `release/drills/at15-*.json`) |
| AT-16 | final live-capital gate | DEPLOYED-ONLY | human promotion using `docs/PROMOTION_CRITERIA.md` + Certification Center; missing evidence = FAIL |

## One-command staging run
```bash
make staging-acceptance             # AT-01 (no downtime)
make staging-acceptance ROLLBACK=1  # + AT-15 forced-failure rollback (minutes of downtime; refuses on STOIC_HOST_ROLE=production)
```
Evidence bundle: `release/drills/summary-<ts>.json` + per-drill files — attach to the promotion review.

## Corrections applied in this round
- P1-1 uptime → `ops_monitoring_coverage_pct` (internal), edge-probe SLI (`trust_stats.SLI`), landing shows availability only when `availability.status == reconciled`.
- P1-2 populations versioned `trust-stats/v2` with definitions, exclusions, environment breakdown, period, as-of, reconciliation, legal-review flag.
- P1-3 outbox ledger (PENDING → conditional mutation stamped `repair_id` → COMPLETED, `replay_incomplete`, unique `correlation_id+kind+batch_hash`, `before`/`after`).
- P1-4 hash chain + `verify` endpoint + app-layer mutation guard with critical alert; renamed "append-only application ledger".
- P1-5 `release_attestation` check in `/api/ops/release-readiness` (tag, SHA, digests, gates, decision, sha/digest match vs running).
- P2-1 all workflow actions SHA-pinned (Dependabot already covers github-actions).
- P2-2 cosign identity pinned to `…/.github/workflows/release.yml@refs/tags/<tag>` + workflow-repository claim; Rekor bundle published/kept.
- P2-3 sweep enumerates accounts ∪ users owning repair candidates.
- P2-4 marquee clones `role="presentation" aria-hidden inert`; axe assertion delegated to the Playwright/testing-agent run.

## Round 4 additions (AT numbering of the round-4 audit)
| AT | Status | Evidence |
|----|--------|----------|
| AT-05 repair outbox fault injection | AUTOMATED | `test_at07_*` (before/after mutation, replay exactly once, timestamps preserved, unledgered mutation impossible) |
| AT-06 repair-chain anchoring | AUTOMATED (app) | `test_at06_anchor_*`: tail deletion → `ledger_sequence_regression`, forged anchor signature, middle edit; anchors exported to `release/ledger-anchors.jsonl` (+ `repair_ledger_anchors`), readiness check `repair_ledger_anchor`. DB-credential denial → **DEPLOYED-ONLY** |
| AT-07 trust-stat populations | AUTOMATED | `test_at11_*`, `test_p22_*` (published only with `TRUST_STATS_LEGAL_APPROVED=true`) |
| AT-08 availability integrity | AUTOMATED | `test_at08_edge_probe_*`: region from token, allowlist, forged eligibility ignored, duplicate minute deduped, per-region coverage gate withholds % |
| AT-10 signed release + rollback | STAGING DRILL | `make staging-acceptance ROLLBACK=1` |
| AT-11 migration host identity | AUTOMATED (unit) | `test_preflight_refuses_without_out_of_band_fingerprint`, `test_scan_observes_without_trusting_then_trust_pins`, `test_host_key_rotation_between_scan_and_trust_is_caught`, step-up on preflight/start/freeze/decommission/abort (`test_iter203/223`) |
| AT-12 migration account reconciliation | AUTOMATED (unit) | `test_full_happy_path_with_gates` (0/1/2/3, wrong identity, unexpected identity → blocked; exact 3 → unlock), `test_cutover_exception_disables_missing_then_unlocks` |
| AT-13 migration rollback/data integrity | PARTIAL | abort restores source once + stops target workers (unit); collection count/hash comparison → **DEPLOYED-ONLY** |
| P1-4 live 3-of-6 | STAGING/PROD RUN | `make staging-acceptance EXPECT=6/3/3` → signed read-only `ops/production_reconcile.py` evidence |
| P2-4 model quality | OUT OF SCOPE of the deploy pipeline — see `docs/MODEL_PROMOTION.md` (to write): walk-forward/OOS, leakage, costs, regime, calibration drift, capacity; model promotion stays independent from application deployment |

## Round 5 — "exact acceptance tests before release" mapping
| # | Test | How | Status |
|---|------|-----|--------|
| 1 | `production_reconcile.py --expect 6/3/3` exit 0, sets identical, no non-boolean flags, no bots on disabled, 3 fresh EAs | `make staging-acceptance EXPECT=6/3/3`; **enforced on every deploy** when `RECONCILE_EXPECT=6/3/3` is in `./.env` (`deploy/update.sh` → rollback on mismatch, signed JSON in `release/evidence/`) | PRODUCTION RUN REQUIRED — not in this package |
| 2 | stale EA heartbeat → reconciliation failure + authority BLOCKED/CLOSE_ONLY | position-truth STALE state (`tests/test_iter200_release_review.py`), readiness `reconciliation` check | AUTOMATED (unit) / drill |
| 3 | broker-accepted unresolved execution older than threshold → no new exposure + UNKNOWN alert | `execution_truth.py` → readiness `execution_truth` 503 (`test_r5_execution_truth_*`, `readiness_drills.py: unresolved_unknown_execution`) | AUTOMATED |
| 4 | broker/API timeout after submission → idempotent retry, exactly one order | `tests/unit/test_authority_matrix.py`, `tests/test_iter193_audit_p0.py` (UNKNOWN never re-dispatches) | AUTOMATED (unit) |
| 5 | corrupt/delete latest repair-ledger row → chain/anchor failure + readiness 503 | `test_at06_anchor_*`, `readiness_drills.py: deleted_ledger_tail` | AUTOMATED |
| 6 | remove external anchor with non-empty ledger → readiness failure | `readiness_drills.py: missing_external_anchor` | AUTOMATED |
| 7 | two edge probes same region/endpoint/minute → one counted, one duplicate | `test_at08_edge_probe_*` | AUTOMATED |
| 8 | forged region/endpoint/eligible → rejected or server-derived | `test_at08_edge_probe_*` | AUTOMATED |
| 9 | wrong fingerprint → no SSH login, no trusted known_hosts, failed preflight | `unit/test_host_migrator.py` (refuse/mismatch/rotation), `test_iter225_audit_p1p2.py` | AUTOMATED |
| 10 | one missing expected EA → decommission blocked | `test_full_happy_path_with_gates`, `test_iter225` | AUTOMATED |
| 11 | DISABLE MISSING → OFF state, 2FA, journal, re-check before decommission | `test_cutover_exception_*`, `test_iter225` | AUTOMATED |
| 12 | expired migration window → 410, no further action | `test_token_guard_and_expiry` | AUTOMATED |
| 13 | stale worker lease + stalled loop → readiness 503, no "ready" UI | `readiness_drills.py` (`stale_worker_lease`, `stalled_loop`); UI reads the same endpoint | AUTOMATED (drill) |
| 14 | attestation for a different SHA/digest → promotion refused | `tests/unit/test_release_attestation.py` (sha/digest mismatch → rc 2), `deploy/lib.sh verify_attestation` | AUTOMATED |
| 15 | revoke performance share + cache invalidation → 404 / unverified payload | revoke → 404 with `Cache-Control: no-store` on both paths (`test_r5_topology_gate_and_monitor_wiring`), performance gate tests | AUTOMATED |
| 16 | frontend build/lint/typecheck/Vitest/E2E/axe | CI `release.yml` frontend job (build+lint); axe + keyboard via testing-agent Playwright runs (iteration_181–184) | PARTIAL — attach CI artifacts |
| 17 | Turnstile pass/fail/timeout/retry, no /welcome fallback for authed routes | `tests/test_iter15x_turnstile*.py`; `synthetic-monitor.yml` flags `/dashboard` rendering the welcome page | PARTIAL — edge paths DEPLOYED-ONLY |
| 18 | public financial claims labeled as platform activity unless legal-approved artifact present | `TRUST_STATS_LEGAL_APPROVED` gate + `context` disclaimer (`test_p22_*`) | AUTOMATED |

**External monitor**: `.github/workflows/synthetic-monitor.yml` (every 5 min from GitHub runners) probes `/api/health`, `/`, `/welcome`, `/login`, `/dashboard` and the login-failure path and feeds `/api/public/edge-probe` with a per-region token (`EDGE_PROBE_TOKENS="gha:<token>"`). Anchor custody beyond the local JSONL + collection (object storage / SIEM shipping, key rotation) remains an ops task.

## Round 6 — release-blocker corrections (mapping to the round-6 findings)

| Finding | Correction | Proof |
|---|---|---|
| P0 protected route fell open to /welcome | `ProtectedRoute` renders exactly one of: app · `/login` boundary (401/403) · `BackendOutage` screen (any other failure: correlation id from `X-Request-ID`, HTTP status, bounded auto-retry 5→30 s, manual retry). Explicit `/dashboard` route; only `/` keeps the public trailer as its logged-out landing. | `tests/test_iter226_audit_round6.py::test_r6_protected_routes_never_fall_back_to_welcome`, browser synthetic `ops/synthetic_browser.py` |
| P0 backend unavailable | Nothing in this repo can prove availability — the release stays BLOCKED until `/api/ops/release-readiness` is 200 from the SAME build (all checks incl. execution truth + topology). | `deploy/update.sh` (rollback on any failure) |
| P1 monitor cadence ≠ SLI | `EDGE_PROBE_CADENCE="gha=300"` declares the GHA prober's cadence; `edge_probes.coverage` expects `window/cadence` per region; `trust_stats.availability_30d.value_pct` is ONLY the 60 s series, 300 s regions are published as `secondary_slis.edge_availability_30d_300s`. Never merged. | `test_r6_region_cadence_explicit_and_coverage_math` |
| P1 6/3/3 gate optional | In production `deploy/update.sh` REQUIRES `RECONCILE_EXPECT` (format `N/N/N`, must equal `RECONCILE_APPROVED_POLICY`, default 6/3/3), `RECONCILE_SCOPE_USER_ID`, `LEDGER_ANCHOR_KEY`; runs `production_reconcile.py --strict --scope-user …`; validates the evidence JSON is PASS + 64-hex signature + known build. Anything missing → rollback. | `test_r6_update_sh_makes_topology_gate_mandatory_in_production` |
| P1 drill destructive w/o guard | `ops/readiness_drills.py` refuses BEFORE the first write unless `APP_ENV=staging` + `ALLOW_DESTRUCTIVE_DRILLS=true` + approved `DB_NAME` suffix + matching `DRILL_STEP_UP_TOKEN`. Faults are tagged disposable rows; anchor/ledger faults insert a bogus anchor (never delete real rows); the only mutated real row (worker lease) is journaled first and `recover()` restores it on the next run (`--recover-only`). | `test_r6_readiness_drill_refuses_production_before_first_write`, `test_r6_drill_recovers_from_a_crashed_run`, `test_r5_readiness_drills_catch_every_injected_fault` |
| P1 execution truth not canonical | `execution_truth.authority_for` is consumed by `trading_authority.execution_domain` (UI ribbon + `enforce_new_trade` choke point), `trading_readiness` (`RECONCILIATION_PENDING` with per-intent/account reasons) and release readiness. UNKNOWN → CLOSE_ONLY immediately; aged states / mismatch → CLOSE_ONLY. | `test_r6_canonical_authority_merges_execution_truth` |
| P1 UNKNOWN timing permissive | Split SLAs: unknown 0 s · submitted/dispatched 120 s · broker_pending/acked 300 s (`EXEC_TRUTH_SLA_S` override); age from `updated_at` → newest `transitions[].at` → `created_at`; BSON datetime + ISO; untimestamped non-terminal = fail closed. | `test_r6_unresolved_uses_last_transition_and_both_timestamp_types` |
| P1 reconciliation scope/signature | `--scope-user` mandatory in strict mode, synthetic/test accounts excluded via `synthetic_data.is_synthetic_account` and listed, `GIT_SHA` must be known, dedicated key only (no JWT fallback in strict), `--expect-ids` exact identity set, unsigned evidence = FAIL. | `test_r6_reconcile_*` |
| P2 raw-HTML SPA monitoring | `browser` job (Playwright, */15) asserts route settlement states; curl job is transport-only. | `.github/workflows/synthetic-monitor.yml` |
| P2 telemetry `|| true` | Ingestion ack validated by `ops/verify_probe_ack.py` (recorded/duplicate, endpoint, region, cadence); telemetry failure is a separate `::error` and non-zero exit. | `test_r6_monitor_separates_cadences_and_telemetry_failures` |

Operator env additions: `RECONCILE_APPROVED_POLICY`, `RECONCILE_SCOPE_USER_ID`, `EDGE_PROBE_CADENCE`, `DRILL_STEP_UP_TOKEN(_EXPECTED)`, `DRILL_DB_SUFFIXES`, `ALLOW_DESTRUCTIVE_DRILLS`, `EXEC_TRUTH_SLA_S`.
Still outside the repo's reach (needs the operator): live backend availability evidence, 30-minute external probe run, 60 s persistent prober for the one-minute SLI, MQL5 EX5 compile, legal review of marketing copy.

## Round 7 — open findings and corrections

| Finding | Correction | Proof |
|---|---|---|
| P1 sweep can mutate an unintended environment | `/api/health` now carries a deployment-SIGNED environment marker (`environment`, `env_sig` = HMAC(LEDGER_ANCHOR_KEY, env\|build)). `tests/test_iter228_route_auth_sweep.py::preflight` refuses BEFORE login unless: marker ∈ staging/ephemeral-test/preview/dev, signature verifies, host is not a production hostname, DB_NAME not production-like, `ALLOW_MUTATING_AUTH_SWEEP=true`, unique `AUTH_SWEEP_RUN_TOKEN` (≥16). Fixtures namespaced by run token; every created object recorded in a signed manifest (`release/evidence/route-auth-sweep-manifest-*.json`); cleanup idempotent from the manifest (also after a forced interruption). | `test_preflight_refuses_production_targets`, `test_manifest_cleanup_survives_forced_interruption` |
| P1 default admin credentials in source | ALL credential literals removed from tests (171 files), seed.py, deploy_preflight.py, scripts, CI. `tests/live_target.py::admin_credentials()` reads `TEST_ADMIN_EMAIL/TEST_ADMIN_PASSWORD` (or `ADMIN_*`) only, refuses known defaults (hash list, no literals) pre-network; conftest aborts live runs (exit 3) otherwise. CI generates a per-run admin password. Preview admins ROTATED — the former default no longer authenticates. Regression test scans source for default literals. | `test_r7_no_default_credential_literals_in_source`, `scripts/verify_release.sh` step `no_default_credentials` |
| P1 incomplete coverage / broad public prefixes | Prefix allowlists replaced by the EXACT manifest `tests/public_routes_manifest.json` (method, path template, expected statuses, classification, review tag). All 39 previously unseeded sensitive params now seeded with real foreign objects (safety blocks, governance changes, PAMM requests/partners, agents, installers, api keys, shadow models, alerts, ledger correlation ids, …); an unseeded sensitive route without an explicit waiver FAILS; waivers must be empty for release. Re-run: 638 ops, 0 leaks. | `test_no_unseeded_sensitive_parameterized_routes`, `test_no_anonymous_leak` |
| P1 production trading truth lacks runtime evidence | `ops/prepromotion_evidence.py`: signed, build-bound, tenant-scoped bundle (hashed account ids, enabled ids, bot map, environments, EA heartbeat consensus + worker-view agreement, exact unresolved executions, position mismatches, platform + per-account authority with UI/enforced agreement, Bot Health cap inputs, performance attestation gate). Fails closed; `deploy/update.sh` runs it in production after the topology gate and rolls back on FAIL. | `test_r7_prepromotion_bundle_refuses_and_fails_closed` |
| P2 ambiguous browser states / console errors ignored | `ops/synthetic_browser.py`: exactly ONE settled state (`AMBIGUOUS_STATE`/`NO_STATE`/`WELCOME_FALLBACK` codes), conflicting authority banners fail, console messages classified (uncaught exception, React boundary/hydration, CSP violation, failed request) and FAIL the run; build SHA attached. | `test_r7_monitor_and_browser_synthetic_strictness` |
| P2 duplicate probes | probe job gated to the `*/5` schedule, browser job to `*/15`; no double submission at 15-minute boundaries. | same test |
| P2 execution backlog caps | `execution_truth.unresolved_backlog()` streams the whole backlog: EXACT totals by status/account, `truncated` flag, oldest 500 exemplars, `newest_unknown_age_s`; authority reasons use exact counts. | `test_r7_backlog_counts_exact_and_truncation_flagged` (5,201 intents) |
| P2 non-reproducible verification | `scripts/verify_release.sh` — one hermetic command: locked backend/frontend deps, SBOMs, compile/syntax, unit + integration tests, lint + build, manifest currency, credential-literal scan, optional guarded sweep; signed results under `release/verification/`. | file present; CI to adopt |

Operator env additions: `TEST_ADMIN_EMAIL`, `TEST_ADMIN_PASSWORD` (CI/staging runners), `ALLOW_MUTATING_AUTH_SWEEP`, `AUTH_SWEEP_RUN_TOKEN`.
Financial-marketing/compliance items remain governed by `trust_stats.legal_review` + attestation gates (no claim is published when reconciliation is stale/incomplete/mixed/unsigned); legal sign-off is external.

## Hermetic verification in the release workflow (round 7 P2 follow-up)
- New `hermetic-verify` job in `.github/workflows/release.yml`: exports the exact archive to a clean runner, then runs `scripts/verify_release.sh` (locked backend + frontend deps, SBOMs, compile/syntax, unit + integration suites, lint + build, test-manifest currency, credential-literal scan). Results are HMAC-signed with the `RELEASE_EVIDENCE_KEY` secret (mapped to `LEDGER_ANCHOR_KEY`), bound to the release commit, and `REQUIRE_SIGNED_RESULTS=1` makes an unsigned run a failure.
- The `release` job needs `hermetic-verify`, re-verifies the signature + PASS + commit binding, feeds the file into `release_attestation.py emit --hermetic-verification`, which adds the `hermetic_verification` gate (REJECTED unless ran · PASS · signed · build matches · zero failed steps). `verify` refuses attestations whose gate is not green. Results and SBOMs ship as release assets (`evidence/hermetic/*`).
- Operator setup: add repository secret `RELEASE_EVIDENCE_KEY` (32+ random bytes; may equal the production `LEDGER_ANCHOR_KEY` so `update.sh` can re-verify evidence on the host).
- Local reproduction: `LEDGER_ANCHOR_KEY=… REQUIRE_SIGNED_RESULTS=1 scripts/verify_release.sh` (≈2 min on a warm machine).

## Round 8 — release-blocking findings: what changed in the repo (and what is operator-only)

| Finding | Repo correction | Operator / external |
|---|---|---|
| P0-1 production Turnstile blocked | `turnstile_gate.py` now separates `client_token_invalid` (403 retryable) · `provider_unavailable` (503, fail-closed; login may degrade ONLY to `otp_required` via `TURNSTILE_LOGIN_DEGRADED_POLICY`) · `configuration_invalid` (503 + critical audit). Server-side `action` + `TURNSTILE_EXPECTED_HOSTNAMES` binding. `TURNSTILE_FORCE_DISABLE` refused in production; audited, ≤4 h `TURNSTILE_BREAK_GLASS_UNTIL/_REASON`. Diagnostics (`/api/ops/turnstile-diagnose`, METRICS_TOKEN path) report state, hostnames, policy, break-glass, build. | Copy `TURNSTILE_SITE_KEY`+`TURNSTILE_SECRET_KEY` from the SAME widget; add every production hostname to the widget allowlist; set `TURNSTILE_EXPECTED_HOSTNAMES`; run the diagnose endpoint with METRICS_TOKEN; verify login/register/reset on the production hostname. |
| P1-2 frontend silent disable | `useTurnstile(action)` explicit states (loading · enabled · disabled-by-policy · configuration-error · script-error · expired · ready); `canSubmit` false while unknown/unavailable; `TurnstileStatus` shows recovery (credentials kept); fixed `action` per surface; reset by widget id. | — |
| P0-2 / P2-2 slow-load boundary | Boot fallback self-dismisses, states "interface unavailable — trading authority cannot be confirmed here; backend safeguards fail closed", probes `/api/health` to say whether the BACKEND is reachable (+ build) or only the bundle failed, includes a support ref. | 3-region browser synthetic on `/dashboard` (ops/synthetic_browser.py in GHA). |
| P1-3 "mTLS" | Mechanism relabelled *pinned secondary identifier* in code; no cryptographic claim. | Terminate client-cert TLS at the edge, strip inbound cert headers, inject verified identity, require enrollment (`AGENT_MTLS_REQUIRED=true`). |
| P1-4 artifact-digest bypass | Route now uses `_mtls_gate` (header `X-Client-Cert-Fingerprint`); source-derived coverage test fails on any agent_token handler bypassing the gate (enrollment is the documented bootstrap exception). | — |
| P1-6 signer contradiction | ONE validator `release_signing.production_signer_violation` used by boot, signer, preflight; `RELEASE_SIGNER_ALLOW_LOCAL_IN_PROD` RETIRED (flagged as misconfiguration); docs fixed. | Configure `RELEASE_SIGNER=external` before any production boot. |
| P1-7 WebAuthn | `/api/auth` sensitive in the sweep; A's passkey seeded; sweep re-run 0 leaks (638 ops). | — |
| P1-5 / P1-8 evidence binding & packaging | `.gitattributes export-ignore`: `test_reports/`, `memory/`, `release/evidence|drills|verification/`, e2e reports never ship in `git archive`; build-bound evidence (hermetic verification, sweep, attestation, pre-promotion) is produced in CI from the tagged commit. | Re-tag the candidate so rc_lock/attestation/evidence all resolve to one commit. |
| P2-1 EA version gate | exactly one NON-EMPTY version == approved (`APPROVED_EA_VERSION` or `LATEST_EA`). | — |
| P2-3 keep-alive | bounded default 75 s (cap 300), overridable/measured. | Address 520/OOM via container limits + metrics. |
| P2-5 marquee clones | decorative clones have no semantic descendants; removed under `prefers-reduced-motion` (verified: 0 semantic nodes in clones, 0 clones reduced-motion). | axe + NVDA/VoiceOver pass. |
| P2-6 copy | "RISK-CONTROLLED AUTOMATED TRADING", no fixed model/provider claim, no "quant fund"/"steady wealth". | Legal sign-off. |
