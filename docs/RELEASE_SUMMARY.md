# Release summary (GENERATED — do not edit; `python scripts/generate_release_summary.py`)

- Source commit: `3c0f6efb5110dc54acb51819f9aadbd518660000`
- rc_lock: `3c0f6efb5110dc54acb51819f9aadbd518660000` authoritative=False
- Model manifest sha256: `b2a5223864b07ecc9528819e9492975eaba3c887ad7c1957461339bd3e6959b6`
- Test manifest sha256: `be3773b6ecd51802fd71dd279d9e47d88b296267850390050c0f7e7f8d2d164d`
- Uvicorn keepalive (container): 75s (application cap 300s)
- Test manifest: 4454 tests (docs/TEST_MANIFEST.md)
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
- P1-05-r15 [P1] Authoritative release artifact: tagged workflow with the external signer on one staged tree → model manifest verifies against the pinned trusted key, strict RC lock, authoritative image digests, signed provenance
- deployed build endpoint verified against that evidence. Cannot be produced from the preview. — owner: release-engineering
- P1-06-r15 [P1] Publication order: images pushed to GHCR under a tag before signer canary / final scans / attestation. Required: build+scan by digest in quarantine, attest, canary, THEN publish the deployable tag once
- admission requires signed provenance. — owner: release-engineering
- P1-07-r15 [P1] Live 6/3/3, broker truth and zero-UNKNOWN closure are unproven on production: authority stays BLOCKED/CLOSE_ONLY and performance publication withheld until a signed pre-promotion + production-reconciliation evidence bundle from the exact deployed digest passes the final gate. — owner: operations
- P2-05-r15 [P2] Preview/prod parity: content-hashed production build on stable staging with non-secret build identity
- Turnstile valid-once/replay/wrong-binding/malformed/skew/outage/script-failure/recovery matrix executed there. — owner: engineering
- P1-03-r16 [P1] Authoritative release identities: BUILD_SHA, RC-lock commit, model-manifest code_commit, image digests, attestation and EA EX5 hash must all bind to ONE peeled tagged commit produced by the tagged workflow (developer-snapshot evidence is not authoritative). — owner: release-engineering
- P1-05-r16 [P1] Live capital-safety acceptance on the exact promoted digest: 6 identity-bound accounts, exactly 3 LIVE enabled + their 3 bots, no orphan/duplicate bots, EA fresh counts agree across inventory/readiness/status/accounts/broker sessions, reconciliation age in SLA, position truth FRESH and matching, zero UNKNOWN/aged executions, Safety Blocks/Bot Health/Start Trader/readiness/authority agree, all performance periods reconcile to signed statements with full coverage. — owner: operations
- P2-01-r16 [P2] Chart provenance: shared SeriesProvenance component + API schema on every chart (source/provider, as-of, timezone, freshness, missing intervals, cache/fallback state, ledger/attestation id)
- timestamp-based ranges
- visual separation of indicative vs simulated vs reconciled broker series. — owner: engineering
- P2-02-r16 [P2] Canonical decision fingerprint completeness: every mutable authority input (global blocks, active alerts, PAMM uncertainty, inventory, identity, executions, reconciliation, Bot Health caps, Safety Blocks) in one snapshot hash/version exposed uniformly in readiness, denials, Bot Health, Safety Blocks, receipts and performance gating. — owner: engineering
- P2-03-r16 [P2] Turnstile/Cloudflare matrix demonstrated on production-like staging with the production-built frontend and a stable hostname (valid-once, replay, wrong action/hostname, malformed, skew, outage, script failure, OTP fallback, recovery, rate limits). — owner: engineering
- P2-04-r16 [P2] Agent transport identity: ingress mTLS with client-certificate verification, rotation/expiry monitoring, revocation and certificate→immutable agent record mapping. — owner: infrastructure
- P1-02-r17 [P1] Authoritative single-commit release: BUILD_SHA, RC lock, model manifest, test manifest, SBOMs, both image digests, EA source + compiled EX5 (hash recorded, signed), signer identity, attestation and deployed /api/health all bound to one peeled tagged commit
- verify offline, deploy by digest pair from release-admission.json. — owner: release-engineering
- P1-03-r17 [P1] Live capital-safety acceptance on the promoted digest in one canonical input version (6 identity-bound accounts, exactly 3 LIVE enabled + their 3 bots, no orphan/null-account bots, EA counts agree everywhere, reconciliation age in SLA, position truth FRESH + matching, zero UNKNOWN/aged executions, Safety Blocks / Bot Health caps / Start Trader / readiness / authority agree, every performance period reconciles to signed statements with full coverage). — owner: operations
- P0-01-r17-infra [P0] Production MongoDB must run as a replica set (transactions) — fenced NL effects fail closed without it
- EA v1.57 must be compiled, hash-recorded and rolled to every terminal so close_idem_key/close_fence are enforced at the destination. — owner: operations
- P2-06-r17 [P2] Turnstile/Cloudflare matrix on a production-built staging bundle at a stable challenge-enabled hostname with the exact build identity recorded. — owner: engineering
- P2-07-r17 [P2] Ingress mTLS for agent transport: client-certificate verification, certificate→agent binding, expiry/rotation monitoring, revocation, fail closed when identity cannot be established. — owner: infrastructure
- P1-03-r18 [P1] Live capital-safety acceptance pack (AT-P1-03..06) on the promoted digest in one canonical input version — 6/3/3 identity-bound inventory, EA counts agree everywhere, reconciliation in SLA, position truth FRESH+matching, zero UNKNOWN executions, blocker dominance across Start Trader/Safety Blocks/Bot Health/readiness/authority, performance reconciled to signed statements. Authority stays BLOCKED/CLOSE_ONLY until proved. — owner: operations
- P1-04-r18 [P1] Authoritative release (AT-P1-07): one peeled tagged commit binds BUILD_SHA, manifests, strict RC lock, SBOMs, both digests, EA MQ5 + compiled EX5 hash/signature (v1.57 — currently unrecorded/unsigned, verify_ea_release fails closed), signer identity, admission pair and deployed /api/health. — owner: release-engineering
- P0-02-r18-handshake [P1] Terminal handshake must PROVE the deployed EX5 hash + capability set (EA reports ea_binary_sha256
- server pins ea_binary_sha256_expected from the verified release) — live_gate() already refuses a mismatch
- the EA-side reporting and server pinning must be wired and rolled out. — owner: engineering

_generated 2026-09-28T09:48:26.178913+00:00 — regenerate on every release commit_
