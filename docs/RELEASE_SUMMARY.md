# Release summary (GENERATED — do not edit; `python scripts/generate_release_summary.py`)

- Source commit: `fff3ff3aee5bef2c1e2eec131478d801d18b6661`
- rc_lock: `fff3ff3aee5bef2c1e2eec131478d801d18b6661` authoritative=False
- Model manifest sha256: `25511e82d03a8a6a3dc1cadd4aa9f1d02be79f8710b4ae663f1135a2262364b2`
- Test manifest sha256: `85c71d3cdd3fc8e08b7a6e518f63ceb6022e2f5623618f07f7543e374f41bdcd`
- Uvicorn keepalive (container): 75s (application cap 300s)
- Test manifest: 4363 tests (docs/TEST_MANIFEST.md)
- Readiness verdict: **NOT RELEASABLE**

## Open findings (docs/open_findings.json)
- P1-07-r9 [P1] Transport mTLS at a client-cert-verifying ingress for agent endpoints (pinned secondary identifier only today) — owner: infrastructure
- P1-09-r10 [P1] Authoritative CI-signed rc_lock for the promoted commit (developer snapshot only until a tagged release run) — owner: release-ci
- P0-03-r10 [P1] Broker-side reconciliation of the six live account IDs + two-admin 6/3/3 inventory approval — owner: operations
- P1-05-r12 [P1] Authoritative release + operational acceptance evidence (6/3/3 inventory, broker truth, zero UNKNOWN executions, EA consistency, Bot Health, performance reconciliation) on ONE signed artifact before any live-capital promotion — owner: operations
- AI-GOV [P2] Model lineage on every AI decision, shadow-only promotion, drift quarantine, broker-realistic backtest costs — owner: engineering
- PERF-ATT [P2] Performance reconciliation to broker statements (fees/financing/deposits) before any public attestation — owner: engineering

_generated 2026-09-27T15:58:33.098341+00:00 — regenerate on every release commit_
