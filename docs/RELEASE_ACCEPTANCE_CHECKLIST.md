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
