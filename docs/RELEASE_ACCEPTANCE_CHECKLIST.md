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
