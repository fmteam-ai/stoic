# Release summary (GENERATED — do not edit; `python scripts/generate_release_summary.py`)

- Source commit: `58f376511118528ae7eba0221047cb94b253320a`
- rc_lock: `58f376511118528ae7eba0221047cb94b253320a` authoritative=False
- Model manifest sha256: `b2a5223864b07ecc9528819e9492975eaba3c887ad7c1957461339bd3e6959b6`
- Test manifest sha256: `a9354a8766898779cae6aab2213d54b98b953483de5c51692128ffbd6de405a2`
- Uvicorn keepalive (container): 75s (application cap 300s)
- Test manifest: 4366 tests (docs/TEST_MANIFEST.md)
- Readiness verdict: **NOT RELEASABLE**

## Open findings (docs/open_findings.json)
- P1-07-r9 [P1] Transport mTLS at a client-cert-verifying ingress for agent endpoints (pinned secondary identifier only today) — owner: infrastructure
- P1-09-r10 [P1] Authoritative CI-signed rc_lock for the promoted commit (developer snapshot only until a tagged release run) — owner: release-ci
- P0-03-r10 [P1] Broker-side reconciliation of the six live account IDs + two-admin 6/3/3 inventory approval — owner: operations
- P1-04-r13 [P1] Signed operational acceptance run on the exact promoted image (6/3/3 inventory, broker truth within SLA, zero UNKNOWN executions, EA-count agreement, Safety Blocks dominance, Bot Health caps, performance reconciliation) — authority stays BLOCKED/CLOSE_ONLY and ML advisory-only until it passes — owner: operations
- AI-GOV [P2] Model lineage on every AI decision, shadow-only promotion, drift quarantine, broker-realistic backtest costs — owner: engineering
- PERF-ATT [P2] Per-account reconciliation ledger whose signed period totals equal broker statements to the cent (commission, swap, deposits/withdrawals, corrections, FX, unrealized) with a versioned cash-flow-adjusted return formula
- any discrepancy withholds attestation — owner: engineering

_generated 2026-09-27T16:14:37.508686+00:00 — regenerate on every release commit_
