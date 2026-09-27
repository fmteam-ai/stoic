# Release summary (GENERATED — do not edit; `python scripts/generate_release_summary.py`)

- Source commit: `ac3dbaef7741a8c6e2353c9bac54ad7499e7fc96`
- rc_lock: `23b9b00776a8ce22c42e7019820176c9a7339d16` authoritative=False model_manifest=4f4658181456…
- Uvicorn keepalive (container): 75s (application cap 300s)
- Test manifest: 4352 tests (docs/TEST_MANIFEST.md)
- Readiness verdict: **NOT RELEASABLE**

## Open findings (docs/open_findings.json)
- P1-07-r9 [P1] Transport mTLS at a client-cert-verifying ingress for agent endpoints (pinned secondary identifier only today) — owner: infrastructure
- P1-09-r10 [P1] Authoritative CI-signed rc_lock for the promoted commit (developer snapshot only until a tagged release run) — owner: release-ci
- P0-03-r10 [P1] Broker-side reconciliation of the six live account IDs + two-admin 6/3/3 inventory approval — owner: operations
- AI-GOV [P2] Model lineage on every AI decision, shadow-only promotion, drift quarantine, broker-realistic backtest costs — owner: engineering
- PERF-ATT [P2] Performance reconciliation to broker statements (fees/financing/deposits) before any public attestation — owner: engineering

_generated 2026-09-27T14:51:50.128538+00:00 — regenerate on every release commit_
