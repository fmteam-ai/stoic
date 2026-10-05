# STOIC — Roadmap

- **P0** Production deployment to stoicaibot.com (K8s timeout on last attempt — retry with `deployment_agent` readiness check; export state from Preview → import to Production for EA token sync).
- **P1** Broker Partnership Setup — `PartnerBrokerCard.jsx` with IB tracking links + free broker VPS guidance.
- **P1** Binance live BTC execution via CCXT.
- **P1** Macro Climate widget on Dashboard (live DXY regime + news-blackout countdown).
- **P2** Surface `mtf_veto` / `auto_tune_block` block reasons inline on Signals page.
- **P2** Per-user slippage analytics widget (avg slippage by symbol/time).
- **P2** Trusted-device "remember this browser for 30 days" for 2FA.
- **P2** Retail-positioning fader (Myfxbook/FXSSI % long XAU contrarian veto).
- **P2** Partial-close ladder + chandelier-exit trail.
- **P2** Walk-forward auto-retune of confluence weights every 4 weeks.
- **P2** Trades page "load more" / server-side date-range beyond 100-row cap.
- **Scalp pre-multi-symbol**: shared AccountScalpRiskState (daily loss/cost/streak/cooldown across symbols) — required before adding GBPUSD.
- **Scalp research**: eval-interval study (100ms–2s), session-bucket validation (early London / overlap / late NY), MFE/MAE conditional target-stop, setup-score bucket shrinkage.
- **Scalp infra**: durable audit event log (dead-letter + flush-on-shutdown), EA report commission/swap fields for exact live cost attribution.

### Post iter-169 (identity corrections DONE 2026-07-25)
- ~~P1 Demo Certification Campaign~~ DONE 2026-07-25 (24/24 scenario runs pass, ledger complete; remaining supervised-live blockers are runtime health gates that clear when terminals reconnect).
- **P1** CI EX5 release pipeline — build EmergentTradingBridge.ex5 in CI, publish to backend/static so /api/ea-script.ex5 serves the signed binary (endpoint + installer verification already live).
- **P2** Real Forex VPS provider sandbox integration (replace provisioning stubs).
- **P1** Distributed Redis rate limiting for enterprise API (priority #6).
- **P0** Implement HOST_AGENT_MSI_SPEC.md (#3) and DOCKER_IMAGE_SPLIT_SPEC.md (#7) when approved.
- **P2** Distributed tracing / OpenTelemetry.
- **P2** Asymmetric artifact signing — HMAC → Ed25519/ECDSA with CI private release keys + rotation.
- **NOTE** Live trading now REQUIRES EA v1.55 + paired installation (identity gate). Existing v1.54 terminals keep sending telemetry but cannot dispatch live trades until re-paired via installer/dashboard.

### PAMM roadmap status (post iter-186, 2026-06)
- ~~P0 Phase 9 Risk Engine (loss caps, drawdown, exposure, correlation, news filter)~~ DONE iter-186
- ~~P0 Phase 10 Broker Health Monitor (heartbeat, latency, 0-100 score)~~ DONE iter-186
- ~~P0 Phase 6 Manager Dashboard (/managed, admin-only)~~ DONE iter-186
- ~~P0 Phase 4 Product Navigation (PRODUCTS sidebar: Bot/Managed/Marketplace/VPS/Analytics)~~ DONE iter-186
- **P1** PAMM Phase 7-8: Strategy Marketplace (public program listings, investor onboarding funnel)
- **P1** Periodic background risk-check + heartbeat loop (currently on-demand via API/dashboard buttons)
- **P1** Manager role UX: open /managed to users with pamm_manager=true (backend already supports; route currently admin-only per user choice)
- **P2** Real broker adapter (MT5 Manager API) implementing BrokerAdapter incl. close_all_positions
- **P2** Event bus Mongo → Redis Streams after self-hosting migration

### External review v52 backlog (post iter-189)
- ~~Batch 1: REDUCE verdict, op-state hierarchy, dual authorization~~ DONE iter-189
- **P1 Batch 2 (AI learning)**: broker rejection taxonomy (MT5 retcodes → strategy/execution/infrastructure/broker-degradation classes); canonical TradeOutcome object (strategy/execution/broker/slippage/latency/regime results) so AI learns from failure CAUSE
- **P1 Batch 3 (DevEx/Ops)**: ./scripts/test-scalp.sh self-contained runner (venv, ephemeral Mongo, migrations, unit+integration, JUnit/coverage); frontend package scripts lint/typecheck/test/test:e2e/build mandatory in CI; unified command center (single GREEN/YELLOW/ORANGE/RED "safe to trade?" aggregating cloud/DB/workers/broker/EA/risk/PAMM)
- **P2 Strategic**: Opportunity Engine above strategies (§23), Strategy Capital Allocator (§24), champion/challenger promotion (§26, extends existing shadow), Broker Intelligence scoring per strategy/symbol (§27, extends broker_intel), requirements split per service (§21, spec exists), final-execution-gate consolidation audit (§6 — verify no path bypasses order_authorization)
- **P2 Verdict tuning**: per-program SOFT_ZONE/MIN_FACTOR knobs; ChangeRequestsPanel expiry countdown

## v62.7 deferred P1s (from user hardening review, June 2026)
- Upgrade factor exposure to SIGNED normalized exposure (currently absolute lots per currency bucket).
- Upgrade slippage monitoring from median-only to distribution-based (p95/p99, regime-aware).
- Add risk_snapshot_id to Outcome Attribution and Trade Intelligence joins.
- Create a stable global risk/error taxonomy (single registry of reason codes).
- Begin O(1) risk-state aggregation design (incremental counters) for Fast/Nitro scale.

### Post iter-190 (Investor Mirroring View DONE 2026-06)
- ~~P1 Investor Mirroring View (read-only /investor)~~ DONE iter-190
- **P1** Investor statements — monthly PDF/CSV of mirrored share + master trades, emailed via Resend.
- **P1** Alert Test Button (notification settings) · Drift Gauge on runner cards · Risk Commander explicit confirmations · Soak memory watch.
- **P2** Metric scope labels on P&L totals · External KMS release signer.
- **P0 (manual)** MQL5 RC compile on Windows → signed EX5 hash via scripts/verify_ea_release.py.

### Post iter-193 (external audit) — deferred architectural items
- **P1-4** TradingDecisionSnapshot: one immutable per-user/account snapshot (inventory, broker freshness, position truth, UNKNOWN executions, EA state, health caps, certification, PAMM, release policy); all UI + execution APIs reference its ID/policy version.
- **P1-5** Tenant scoping: declare platform-global vs tenant-scoped domains; require user_id/tenant_id in schemas/indexes/query helpers; lint against unscoped reads.
- **P1-7** Split ML inference out of the API process; container CPU/mem limits, startup vs readiness probes, bounded queues, restart/rollback alerts.
- **P0-3** Deployment provenance: fail + auto-rollback when running SHA ≠ signed release; image digest / schema version on /api/status; Admin provenance card.
- **P2-2** Hermetic <5-min safety suite on public deps (authority, state contract, reconciliation, risk invariants).
- **P2-3** Exception taxonomy: replace safety-path `pass` with typed outcomes + correlation IDs.
- **P2-4** Jurisdiction-specific legal review before public launch.

## After audit round 9 (2026-06)
- P1-07 transport mTLS: needs an ingress/proxy that terminates TLS, verifies client certs against the private CA, strips inbound identity headers and injects verified identity; then make `_mtls_gate` REQUIRE it in production (config + tests exist for the pinned secondary identifier only). Operator/infra work.
- Frontend corrections still open: typed UI state model (loading/stale/partial/blocked/degraded/unavailable/ready) across every trading-critical number with source/broker timestamps + reconciliation age; route-level error boundaries distinguishing auth/outage/bundle failures; visual-regression + a11y + mobile tests in release evidence; sweep remaining client-side derivations of ready/executing/free/certified → consume /api/authority/decision.
- Backend corrections still open: singleton background-worker ownership exposure (current lease owner endpoint); AI decision lineage fields (model version, training window, feature schema, owner, approval, expiry, rollback target) + quarantine on drift/missing features; backtest cost model completeness (spread/slippage/commission/swap/latency/rejects/partial fills/survivorship/leakage); explainability tied to actual gates; portfolio-level composition of strategy limits.
- Staging candidate drills (rollback, broker timeout, late-ACK, stale-position, Turnstile outage, memory pressure) with the signed 6/3/3 pre-promotion bundle.

## Operator "STOIC Improvement Roadmap" (PDF, 2026-10-02) — 20 steps, 6 phases, 1 PR each, 1–2 days demo soak between
Fingerprint rule per step: `python scripts/generate_test_manifest.py` → `python scripts/freeze_rc_lock.py`. Deploy: `STOIC_READINESS_POLICY=onboarding-close-only ./deploy/update.sh` (check `ls -t backups/ | head -3` first).
- Phase 1 Trading correctness: **1 C1 calibration (DONE 2026-10-02)** · **2 sizing H3-H5 (DONE 2026-10-02)** · **3 reconciliation C3,C4 (DONE 2026-10-03)** · **4 fail-closed risk C2,H6,H7 + Calibration Health Card (DONE 2026-10-03)** · **5 symbol matching H1,H2 (DONE 2026-10-03)** · **6 signal data H8-H11 (DONE 2026-10-03)**
- Phase 2 AI: 7 central model settings (exists on GitHub as reverted PR #17: llm_client.py/llm_models.py; LLM_MODEL_FAST=claude-haiku-4-5-20251001) · 8 AI safety H12-H14
- Phase 3 Security (no migrations): 9 step-up/passkey/Telegram H15-H17 (set WEBAUTHN_ORIGIN) · 10 worker lease H18 · 11 real client IPs + separate deploy token
- Phase 4 Quant (~1 week each): 12 purged CV/DSR/PBO/cost model · 13 broker instrument specs · 14 HMM regimes + conformal bands · 15 meta-labelling
- Phase 5: 16 EA v1.58 server part first, then one demo terminal (Windows compile + verify_ea_release.py --sign)
- Phase 6 when needed: 17 billing idempotency · 18 crypto SL lifecycle · 19 ▲ login/token hardening (hashed terminal tokens — caused 2 Oct rollback; DB restore needed on undo) · 20 Node 22/uvicorn/image pinning

## Operator "STOIC Fix Plan" (PDF, 2026-10-03) — 63 findings, 11 steps, batches A–F, one PR each + 1–2 day soak
- **A1 P&L after partial closes (B1,B8) — DONE 2026-10-03**
- **A2 order lifecycle & PANIC (B2, B3/R4, B4, B7) — DONE 2026-10-03** (operator after deploy: check log for `unique ticket index … ready`; if refused run `ops/ticket_duplicates.py`)
- **A3 ticket index & order expiry (live-only index, netting leg key, duplicate merge, dispatched expiry, --archive safety) — DONE 2026-10-03**
- **A5 review fixes (first-dispatch expiry anchor, late-fill duplicate absorb, user vs admin lock, --archive actually archives, test DB safety) — DONE 2026-10-03**
- **B1 sizing (R1 JPY-cross notional, R9 trims skip below min, R10 price-aware pip + broker volume step, B10 authority floor, B11 paper P&L/single settlement) — DONE 2026-10-03**
- **A4 PANIC, locks & exit guards (late-fill close, scoped release + /admin/panic/release, demo w/o MFA, lock on dashboard/Telegram/copilot, TP1 banked through reconciler) — DONE 2026-10-03**
- **B2 safety guards (R2 opened_at, R5 clamp fails closed, R6 heartbeat/quote block, R7, R8, B6 manual price check, R14) — DONE 2026-10-05**
- **Security & Health Agent SA1–SA4 — DONE 2026-10-05** (observe mode until operator sets SECURITY_AGENT_PROTECTED_IPS + Telegram secrets and enables rules one at a time; SA5 hardening extras pending)
- C1 candle data (A1 per-user/timeframe, R3 broker UTC offset, A3, A10, A11, R11) · C2 AI calls/gates (A2 timeouts, A4, A5, A6, A7, A8, A9, A12, A13, A14, R12) — covers roadmap step 8
- D1 logs/small fixes (S4, S5, S8 needs RESEND_API_KEY, S9) · D2 2FA/passkeys/admin (S1, S2, S6, S10, S11; set WEBAUTHN_RP_ID/ORIGIN)
- E1 sessions/navigation (F1, F5, F6, F12) · E2 pages/data freshness (F2, F3, F4, F7, F8, F9, F10, F11)
- F1 rate limits/backups/deploy safety (S3, S7, S12 encrypted backups passphrase, S13, S14, R13)
- Deferred to roadmap step 16 (EA v1.58): B5 free margin, B9 spreads.
