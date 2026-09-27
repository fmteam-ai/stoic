# Release summary (GENERATED — do not edit; `python scripts/generate_release_summary.py`)

- Source commit: `bb720f7be3a87b3c86fa4185688040a8ce2a0373`
- rc_lock: `cdf4a35d5954eda1b37a148fabec1a25790682cb` authoritative=False
- Model manifest sha256: `b2a5223864b07ecc9528819e9492975eaba3c887ad7c1957461339bd3e6959b6`
- Test manifest sha256: `0b953b2dcebc8f1c0aa20cd2020f0891e80bde18d6bae474063a99df42b8a8d3`
- Uvicorn keepalive (container): 75s (application cap 300s)
- Test manifest: 4383 tests (docs/TEST_MANIFEST.md)
- Readiness verdict: **NOT RELEASABLE**

## Open findings (docs/open_findings.json)
- P1-07-r9 [P1] Transport mTLS at a client-cert-verifying ingress for agent endpoints (pinned secondary identifier only today) — owner: infrastructure
- P1-09-r10 [P1] Authoritative CI-signed rc_lock for the promoted commit (developer snapshot only until a tagged release run) — owner: release-ci
- P0-03-r10 [P1] Broker-side reconciliation of the six live account IDs + two-admin 6/3/3 inventory approval — owner: operations
- P1-04-r13 [P1] Signed operational acceptance run on the exact promoted image (6/3/3 inventory, broker truth within SLA, zero UNKNOWN executions, EA-count agreement, Safety Blocks dominance, Bot Health caps, performance reconciliation) — authority stays BLOCKED/CLOSE_ONLY and ML advisory-only until it passes — owner: operations
- AI-GOV [P2] Model lineage on every AI decision, shadow-only promotion, drift quarantine, broker-realistic backtest costs — owner: engineering
- PERF-ATT [P2] Per-account reconciliation ledger whose signed period totals equal broker statements to the cent (commission, swap, deposits/withdrawals, corrections, FX, unrealized) with a versioned cash-flow-adjusted return formula
- any discrepancy withholds attestation — owner: engineering

_generated 2026-09-27T18:08:15.876427+00:00 — regenerate on every release commit_
