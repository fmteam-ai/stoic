# Release summary (GENERATED — do not edit; `python scripts/generate_release_summary.py`)

- Source commit: `5814099f4477d3eee0acdc8b41362167bdaeadf2`
- rc_lock: `5814099f4477d3eee0acdc8b41362167bdaeadf2` authoritative=False
- Model manifest sha256: `b2a5223864b07ecc9528819e9492975eaba3c887ad7c1957461339bd3e6959b6`
- Test manifest sha256: `22a68cbd9a3d601ebbfb69755d20cae31cf2a2a939b977cb5ac1c7030afdd762`
- Uvicorn keepalive (container): 75s (application cap 300s)
- Test manifest: 4392 tests (docs/TEST_MANIFEST.md)
- Readiness verdict: **NOT RELEASABLE**

## Open findings (docs/open_findings.json)
- P1-07-r9 [P1] Transport mTLS at a client-cert-verifying ingress for agent endpoints (pinned secondary identifier only today) — owner: infrastructure
- P1-09-r10 [P1] Authoritative CI-signed rc_lock for the promoted commit (developer snapshot only until a tagged release run) — owner: release-ci
- P0-03-r10 [P1] Broker-side reconciliation of the six live account IDs + two-admin 6/3/3 inventory approval — owner: operations
- P1-04-r13 [P1] Signed operational acceptance run on the exact promoted image (6/3/3 inventory, broker truth within SLA, zero UNKNOWN executions, EA-count agreement, Safety Blocks dominance, Bot Health caps, performance reconciliation) — authority stays BLOCKED/CLOSE_ONLY and ML advisory-only until it passes — owner: operations
- AI-GOV [P2] Model lineage on every AI decision, shadow-only promotion, drift quarantine, broker-realistic backtest costs — owner: engineering
- PERF-ATT [P2] Per-account reconciliation ledger whose signed period totals equal broker statements to the cent (commission, swap, deposits/withdrawals, corrections, FX, unrealized) with a versioned cash-flow-adjusted return formula
- any discrepancy withholds attestation — owner: engineering
- P1-04-r14 [P1] Release package not release-ready: one staged tree for one peeled commit, model manifest signed by the trusted external key, test manifest/RC lock/release summary regenerated from that tree, images built from it, evidence extracted+compared, final image digests in an external signed provenance statement. Cannot be produced from the preview (needs the tagged release workflow + external signer). — owner: release-engineering
- P1-07-r14 [P1] Publication order: build+scan by digest first, keep images quarantined/private, run signer canary and all final gates, THEN attach the release tag/alias
- consumers deploy only from signed provenance, never a mutable tag. Workflow reordering pending in release.yml. — owner: release-engineering
- P2-02-r14 [P2] Charts: per-series metadata (provider/source, as-of, timezone, freshness, gaps, fallback/cache) and timestamp-based calendar windows
- indicative market history visually distinct from reconciled broker performance. — owner: engineering
- P2-03-r14 [P2] Canonical decision fingerprint must cover every mutable authority input (global trading-block, alert/PAMM state) or derive one decision hash from a complete canonical snapshot
- input version exposed in readiness, Bot Health, Safety Blocks and denial receipts (partially exposed today). — owner: engineering
- P2-04-r14 [P2] Preview/prod parity: production-built staging bundle with content hashes + non-production banner, Turnstile enabled on a stable staging hostname, /api/version equals the promoted build
- full Turnstile matrix (valid-once, replay, wrong action/hostname, skew, outage fallback, script failure) run there. — owner: engineering

_generated 2026-09-27T19:39:33.528190+00:00 — regenerate on every release commit_
