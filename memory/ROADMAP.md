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
- **P1** Demo Certification Campaign — run the 15-scenario demo order campaign so supervised-live unlocks with real evidence.
- **P1** CI EX5 release pipeline — build EmergentTradingBridge.ex5 in CI, publish to backend/static so /api/ea-script.ex5 serves the signed binary (endpoint + installer verification already live).
- **P2** Real Forex VPS provider sandbox integration (replace provisioning stubs).
- **P2** Asymmetric artifact signing — HMAC → Ed25519/ECDSA with CI private release keys + rotation.
- **NOTE** Live trading now REQUIRES EA v1.55 + paired installation (identity gate). Existing v1.54 terminals keep sending telemetry but cannot dispatch live trades until re-paired via installer/dashboard.
