# STOIC — Deployment Guide

## Topology
`docker-compose.yml` runs: MongoDB, backend API (:8001), **five dedicated
workers** (trading, protection, reconciliation, analytics, model), frontend
(:3000, nginx serving the React build and proxying `/api`).

## One-command install
```bash
deploy/install.sh
```
Does: prereq checks → creates `backend/.env` from `.env.example` with generated
JWT/metrics secrets → `docker compose build && up -d` → API health wait →
worker status print.

## Updating
```bash
deploy/update.sh            # latest origin/main
deploy/update.sh v1.4.0     # specific signed release tag
```
Backs up Mongo first, checks out the ref, rebuilds, restarts, verifies health
— and **rolls back automatically** to the previous commit if health fails.

## Backups
```bash
deploy/backup.sh backup           # timestamped gzip archive in ./backups (14-day retention)
deploy/backup.sh restore <file>   # destructive restore
deploy/backup.sh schedule         # prints the nightly crontab line
```
Weekly restore drills into a staging copy are part of the soak plan
(docs/MT5_VALIDATION_CAMPAIGN.md §7).

## Production configuration checklist
Set in `backend/.env` (see `.env.example` for every key):

| Key | Requirement |
|---|---|
| `APP_ENV` | `production` — activates startup guards |
| `CSRF_ENFORCE_ORIGIN` | `true` |
| `CORS_ORIGINS` | explicit allowlist (never `*`) |
| `JWT_SECRET` / `JWT_REFRESH_SECRET` | long random values (installer generates) |
| `MONGO_URL` / `DB_NAME` | your cluster |
| `METRICS_TOKEN` | random — gates Prometheus scrape |
| `STEP_UP_BYPASS_TOKEN` / `RATE_LIMIT_BYPASS_TOKEN` | **MUST be unset** — startup hard-fails otherwise |
| `BACKGROUND_WORKERS_IN_PROCESS` | `false` on the API (compose sets this) |
| Integrations | `EMERGENT_LLM_KEY`, `NEWSAPI_KEY`, `TELEGRAM_*`, `RESEND_*`, `STRIPE_API_KEY` as needed |

Frontend build arg: `REACT_APP_BACKEND_URL` = the public origin.

## Deployment verification checklist
1. `curl -fsS https://<host>/api/health` → 200.
2. `docker compose ps` → all 5 workers `Up`; logs show lease acquisition.
3. Login works; **Trading Safety banner** shows MODE/RECON/PANIC pills.
4. Bot Health → Execution Health: Mongo latency, workers, outbox all green.
5. `curl -H "X-Metrics-Token: …" https://<host>/api/metrics` → Prometheus text.
6. Register with `password123` → rejected (`breached_password`) — HIBP live.
7. Attempt live activation → step-up MFA modal appears.
8. EA on a demo terminal: heartbeat arrives, account certification matrix green.
9. `deploy/backup.sh backup` produces an archive; test restore on staging.
10. Verify the release: `cosign verify-blob --signature release-manifest.json.sig
    --certificate release-manifest.json.pem release-manifest.json` and compare
    `SHA256SUMS`.

## Releases
Tag `v*` → `.github/workflows/release.yml`: runs the suite **from the exact
archive**, builds images + SBOM, writes `release-manifest.json` (commit, EA
version, artifact hashes), signs manifest + SHA256SUMS with cosign (keyless),
publishes a GitHub release. CI (`ci.yml`) separately gates every push:
tests, real MetaEditor EA compile (verified `.ex5` artifact), blocking
pip-audit/gitleaks/image scans.

## Related documents
- `docs/RUNBOOK.md` — day-2 operations
- `docs/DISASTER_RECOVERY.md`, `docs/ROLLBACK.md`, `docs/INCIDENT_RESPONSE.md`
- `docs/MT5_VALIDATION_CAMPAIGN.md` — pre-live broker validation + soak
