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
