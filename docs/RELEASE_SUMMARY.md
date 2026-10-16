# Release summary (GENERATED — do not edit; `python scripts/generate_release_summary.py`)

- Source commit: `c0146808f1ece8ba2aba075eb4b3c3262404f2b0`
- rc_lock: `c0146808f1ece8ba2aba075eb4b3c3262404f2b0` authoritative=False
- Model manifest sha256: `f04d8e4799590c3c80e2f346295e966b2876b49c8803eb04fe1e88a0cf535cb4`
- Test manifest sha256: `f779e9f040d86e376e0229f7d34b5fa55bd4e2fb342892d2cce6d6e10e3c2bbe`
- Uvicorn keepalive (container): 75s (application cap 300s)
- Test manifest: 6082 tests (docs/TEST_MANIFEST.md)
- Readiness verdict: **NOT RELEASABLE**

## Open findings (docs/open_findings.json)
- AI-GOV [P2] Model lineage on every AI decision, shadow-only promotion, drift quarantine, broker-realistic backtest costs — owner: engineering
- PERF-ATT [P2] Per-account reconciliation ledger whose signed period totals equal broker statements to the cent (commission, swap, deposits/withdrawals, corrections, FX, unrealized) with a versioned cash-flow-adjusted return formula
- any discrepancy withholds attestation — owner: engineering
- P1-06-r15 [P1] Publication order: images pushed to GHCR under a tag before signer canary / final scans / attestation. Required: build+scan by digest in quarantine, attest, canary, THEN publish the deployable tag once
- admission requires signed provenance. — owner: release-engineering
- P2-05-r15 [P2] Preview/prod parity: content-hashed production build on stable staging with non-secret build identity
- Turnstile valid-once/replay/wrong-binding/malformed/skew/outage/script-failure/recovery matrix executed there. — owner: engineering
- P2-01-r16 [P2] Chart provenance: shared SeriesProvenance component + API schema on every chart (source/provider, as-of, timezone, freshness, missing intervals, cache/fallback state, ledger/attestation id)
- timestamp-based ranges
- visual separation of indicative vs simulated vs reconciled broker series. — owner: engineering
- P2-02-r16 [P2] Canonical decision fingerprint completeness: every mutable authority input (global blocks, active alerts, PAMM uncertainty, inventory, identity, executions, reconciliation, Bot Health caps, Safety Blocks) in one snapshot hash/version exposed uniformly in readiness, denials, Bot Health, Safety Blocks, receipts and performance gating. — owner: engineering
- P0-01-r17-infra [P0] Production MongoDB must run as a replica set (transactions) — fenced NL effects fail closed without it
- the shipped EA (≥ 1.57 — production runs the current signed EA, see /api/health ea_version) must be hash-recorded and rolled to every terminal so close_idem_key/close_fence are enforced at the destination. — owner: operations
- P2-06-r17 [P2] Turnstile/Cloudflare matrix on a production-built staging bundle at a stable challenge-enabled hostname with the exact build identity recorded. — owner: engineering
- P2-07-r17 [P2] Ingress mTLS for agent transport: client-certificate verification, certificate→agent binding, expiry/rotation monitoring, revocation, fail closed when identity cannot be established. — owner: infrastructure
- P1-03-r18 [P1] Live capital-safety acceptance pack (AT-P1-03..06) on the promoted digest in one canonical input version — 6/3/3 identity-bound inventory (= the FUTURE live-capital policy target
- the single ACTIVE policy is demo-rules-v2, demo-only), EA counts agree everywhere, reconciliation in SLA, position truth FRESH+matching, zero UNKNOWN executions, blocker dominance across Start Trader/Safety Blocks/Bot Health/readiness/authority, performance reconciled to signed statements. Authority stays BLOCKED/CLOSE_ONLY until proved. — owner: operations
- P1-04-r18 [P1] Authoritative release (AT-P1-07): one peeled tagged commit binds BUILD_SHA, manifests, strict RC lock, SBOMs, both digests, EA MQ5 + compiled EX5 hash/signature (v1.57 — currently unrecorded/unsigned, verify_ea_release fails closed), signer identity, admission pair and deployed /api/health. — owner: release-engineering
- SEC-002-r19 [P3] Cloudflare edge duplicates security headers on /api/* (two HSTS max-ages, doubled Referrer-Policy / X-Content-Type-Options). Scope the Transform Rule to non-/api paths or use Set-static (overwrite) and align the dashboard HSTS toggle to 12 months — docs/CLOUDFLARE_EDGE.md §8. — owner: operations
- P1-01-r20-ea-proof [P1] Terminal-side binary proof: the EA must report its EX5 hash (and capability bitmap / build commit) on the heartbeat handshake, nonce-bound. MQL5 sandboxing prevents an EA from hashing its own EX5, so the proof must come from a signed installer/agent step or an operator-attested install record bound to the terminal identity
- until then live authority stays CLOSE_ONLY (EA_BINARY_PROOF_MISSING). — owner: engineering
- P2-01-r20-close-writers [P2] Migrate the remaining close writers (protection_guard, position_protector, eod_flatten, friday_flat, portfolio/risk_manager, trade_manager, trade_reconciler, integrity, telegram/trade/diagnostic routes) to close_commands.request_close() / cancel_pending() so every broker close has one sequence namespace and ledger row. — owner: engineering
- P1-02-r20-capability-record [P2] Replace the NL_EFFECTS_SYNTHETIC_ONLY environment flag with a signed/immutable deployment capability record and refuse capital-capable startup when the transaction probe fails. — owner: engineering
- SEC-001-r21-ea-proof-challenge [P3] EA binary proof is now admitted only over a verified installation chain (r21)
- a signed server-nonce challenge from the terminal/installer would additionally prove possession of the binary rather than the hash value. — owner: engineering
- P3-r22-pairing-rate-limit-review [P3] Tune BRIDGE_IP_LIMIT_PER_MIN (default 1200) per VPS fleet size once live heartbeat volume per address is measured
- claim-pairing is 20/10 min per IP. — owner: operator
- P3-r23-pamm-amount-check [P3] PAMM add_investor: add an explicit server-side positive/precision amount check ahead of the broker adapter (adapter currently the only guard). — owner: engineering
- OPS-r24-signer-deferred [P1] Production runs with RELEASE_SIGNER deferred (nothing signed
- live exposure CLOSE_ONLY via RELEASE_SIGNER_DEFERRED gate). Provision the external signer (deploy/signer/init_fly_signer.sh (first time) / flyctl deploy (redeploy)), set RELEASE_SIGNER=external + URL/ALLOWED_HOSTS/TOKEN/KEY_ID/PUBLIC_KEY in Secrets, then set RELEASE_SIGNER_DEFERRED=false. — owner: operator
- OPS-r24-provenance-source-digest [P2] Managed publish injects no image digest: production identity is the source-tree digest (PROVENANCE_KIND=source-tree). Acceptable
- inject STOIC_IMAGE_DIGEST/STOIC_BUILD_SHA if the platform ever exposes them. — owner: operator
- OPS-r24-turnstile-hostnames [P3] TURNSTILE_EXPECTED_HOSTNAMES in Secrets includes the preview host so the shared .env works in both environments
- tighten to the production hosts only in the Secrets tab. — owner: operator

_generated 2026-10-16T08:36:41.122979+00:00 — regenerate on every release commit_
