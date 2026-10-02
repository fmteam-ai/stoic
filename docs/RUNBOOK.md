# STOIC Production Runbook

## Services
| Component | Runtime | Health |
|---|---|---|
| Backend (FastAPI + workers) | port 8001 | `GET /api/health` |
| Frontend (React build) | port 3000 | `/` returns 200 |
| MongoDB | 27017 | `db.adminCommand('ping')` |
| MT5 EA bridge | user terminals | `GET /api/bot/execution-health` → `infra.heartbeats` |

## Daily checks (operator)
1. Dashboard **Trading Safety banner** must be green (`safe`). Anything else → follow Incident Response.
2. Bot Health → Execution Health panel: outbox pending 0, no stuck dispatches, all EA heartbeats fresh (<300s), latency p95 within norm.
3. Accounts → every live account shows **CERTIFIED** (all 11 go-live checks).

## Monitoring
- Prometheus scrape: `GET /api/metrics` with header `X-Metrics-Token: $METRICS_TOKEN`.
- Token split (O8): `METRICS_TOKEN` is **read-only** (metrics + GET `/api/ops/*`).
  Mutating machine calls (POST `/api/ops/*`, e.g. release promote/rollback,
  drills, alert ack, validation evidence) need
  `X-Ops-Deploy-Token: $(cat secrets/ops_deploy_token)` (`deploy/lib.sh`
  → `ops_deploy_token`). Rollout: run `deploy/update.sh` (generates the secret),
  switch external tooling, keep `OPS_ALLOW_METRICS_TOKEN_FOR_DEPLOY` unset.
  Human admins need role admin **with TOTP MFA** (plus step-up where noted).
  Key series: `stoic_unprotected_open`, `stoic_unresolved_submissions`,
  `stoic_outbox_pending`, `stoic_ea_heartbeat_age_seconds`, `stoic_worker_lease_alive`,
  `stoic_mongo_latency_ms`, `stoic_ws_clients`.
- Suggested alerts:
  - `stoic_unprotected_open > 0` for 2m → page
  - `stoic_unresolved_submissions > 0` for 10m → page
  - `stoic_ea_heartbeat_age_seconds > 300` → warn (terminal offline)
  - `stoic_outbox_pending > 10` for 5m → warn
- Structured access logs: JSON lines from the `access` logger with `rid`
  (X-Request-ID, propagated from the client if provided).

## Releases
1. CI must be green (backend tests, EA structural + version gate, frontend build, scans).
2. Compile `backend/static/EmergentTradingBridge.mq5` in MetaEditor (Windows):
   `metaeditor64.exe /compile:EmergentTradingBridge.mq5 /log` → require "0 errors".
3. Bump only via the version-consistency set (EA, bot_routes, diagnostic_routes,
   setup_routes, Accounts.jsx, EaVersionStrip.jsx) — `scripts/check_ea_structure.py` gates drift.
4. `FENCING_MIN_EA` is the live floor: raise it ONLY after all user terminals updated.
5. Deploy backend before asking users to update EAs (backend is backward compatible per version gate).

## Secrets
All configuration via `backend/.env` (see `.env.example`). Broker/exchange
secrets at rest are encrypted with `KEY_VAULT_MASTER`. Rotate `JWT_SECRET`
only during a maintenance window (invalidates all sessions).
