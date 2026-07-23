# STOIC — AI Trading Platform

Event-driven quantitative trading platform for Gold, Indices, FX and Crypto.
React terminal + FastAPI backend + MongoDB event ledger, executing through a
MetaTrader 5 Expert Advisor bridge (and a CCXT crypto bridge), with a shared
execution kernel, explainable AI decisions and veto-first guardrails.

> **RISK WARNING** — Trading leveraged products carries a high risk of loss
> exceeding your deposit. STOIC is **software, not investment advice**; it
> places orders with **your** broker under **your** configuration. Past or
> simulated performance never guarantees future results. Complete the full
> MT5 validation campaign and the staged rollout (shadow → demo → small
> supervised live) before any real capital, and never trade money you cannot
> afford to lose.

## Architecture

```
┌──────────────┐   HTTPS (Caddy TLS)   ┌───────────────────────────────┐
│ React 19 SPA │ ─────────────────────▶│ FastAPI backend  /api/*       │
│ (terminal UI)│                       │  auth (JWT+TOTP step-up)      │
└──────────────┘                       │  strategy + scalp engines     │
                                       │  execution kernel + outbox    │
┌──────────────┐  bridge token (poll)  │  ops: readiness/alerts/stage  │
│ MT5 EA v1.5x │ ◀────────────────────▶│                               │
│ (.ex5, CI-   │  heartbeat/report     └──────┬────────────────────────┘
│  compiled)   │                              │ MongoDB (event ledger:
└──────────────┘                              │  trade_events, decisions,
                                              ▼  outbox, journal, alerts)
                    6 leased workers: trading · protection · reconciliation
                    · analytics · model · tuning  (leader leases + per-loop
                    progress telemetry, crash/stall detection)
```

Key subsystems: multi-timeframe strategy engine (per account), dedicated
scalp fast path, profit-protection suite, panic/circuit breakers, immutable
`trade_events` ledger, AI trade journal, Prometheus metrics, ops alerting,
staged live-deployment gate. Detailed docs live in [`docs/`](docs/):
[ARCHITECTURE](docs/ARCHITECTURE.md) · [API](docs/API.md) ·
[RUNBOOK](docs/RUNBOOK.md) · [DEPLOYMENT](docs/DEPLOYMENT.md) ·
[DISASTER_RECOVERY](docs/DISASTER_RECOVERY.md).

## Installation

Requirements: Docker + Compose v2, a Linux host, and for production a DNS
name pointing at the host.

```bash
# development (loopback-only ports)
deploy/install.sh --dev

# production (public TLS via Caddy, auto-provisioned certificate)
deploy/install.sh --production trade.example.com
```

The installer generates Docker secrets (`./secrets/`), builds the 9-container
topology and **refuses to finish** until the full release-readiness gate is
green: HTTPS/frontend reachable, MongoDB round trip, all six workers leased,
every loop showing recent progress, reconciliation fresh, outbox healthy and
schema compatible. The seeded admin uses a one-time bootstrap password that
must be changed on first login.

Updates and rollbacks:

```bash
deploy/update.sh v1.7.0        # backup → deploy → verify → auto-rollback on failure
deploy/rollback.sh             # one-command return to the previous release
deploy/backup.sh schedule      # nightly encrypted backups + restore verification
```

## Connecting a broker (MT5)

1. Create an account entry in **MT5 Accounts** — a unique bridge token is
   generated per account.
2. Download the EA from the app (or use the release-attached, CI-compiled
   `EmergentTradingBridge.ex5`) and attach it to one chart in your MT5
   terminal with the bridge token + backend URL.
3. Both **netting** and **hedging** account types are supported; partial and
   multi-deal fills, external/manual closes and restarts are reconciled from
   broker truth (deal history replay + intent journal).

Any MT5 broker works. Validation evidence has been gathered on IC Markets /
RoboForex-style demo servers; run [the validation campaign]
(docs/MT5_VALIDATION_CAMPAIGN.md) against **your** broker before live.
Crypto spot/perp goes through the CCXT bridge (Binance et al., off by
default: `BINANCE_LIVE_ENABLED=false`).

## Testing

```bash
cd backend && python -m pytest             # full suite (~2700 tests)
python -m pytest tests/unit -q             # fast unit slice
python -m pytest tests/integration -m integration -q   # real-MongoDB slice
cd e2e && npx playwright test              # real-browser UI suite
python scripts/generate_test_manifest.py   # regenerate docs/TEST_MANIFEST.md
```

Every test is classified in [`docs/TEST_MANIFEST.md`](docs/TEST_MANIFEST.md);
CI fails if the manifest drifts from the tree.

## Release verification

A git tag `v*` triggers [.github/workflows/release.yml]
(.github/workflows/release.yml), which blocks publication until, on the exact
tagged commit: the complete CI suite passes (unit, integration, frontend
build, Playwright UI, gitleaks, pip-audit, ruff), Docker images build and
pass Grype vulnerability scanning, the classified suite passes **from the
exported archive**, the real installer brings up the full topology and
reports `release-readiness: ready`, and the EA compiles with real MetaEditor
(`0 errors`). The release then ships: the source archive, the verified
`.ex5`, SPDX SBOMs, a signed manifest + SHA256SUMS (Sigstore keyless) and
container images published to GHCR and **cosign-signed by immutable digest**.

## Operations

- `GET /api/ops/release-readiness` — full-topology health verdict
- `GET /api/metrics` — Prometheus exposition (METRICS_TOKEN)
- `GET /api/ops/alerts` — deduped ops alerts with acknowledgement
- `GET /api/ops/stage` — staged rollout gate (shadow → … → production)
- Bot Health page — readiness, alerts, validation campaign and stage cards

## License / commercial use

Proprietary. All rights reserved. Contact the maintainers for licensing.
