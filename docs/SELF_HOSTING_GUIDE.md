# STOIC — Self-Hosting Guide (migrating from Emergent to your own server)

This is the end-to-end playbook for running STOIC on your own hardware and
pointing **www.stoicaibot.com** at it. It complements `docs/DEPLOYMENT.md`
(reference) with the exact migration order. Time budget: ~1–2 hours, with a
zero-risk rollback (DNS flip back) at every step until the final cutover.

---

## 1. Server requirements

| Resource | Minimum | Recommended |
|---|---|---|
| OS | Ubuntu 22.04/24.04 LTS (Debian 12 also fine) | same |
| CPU | 2 vCPU | 4+ vCPU |
| RAM | 4 GB | 8–16 GB (the Emergent pod's 1 GB was the pain point) |
| Disk | 40 GB SSD | 80+ GB SSD (Mongo data + backups + Docker images) |
| Network | public IPv4, ports **80/443** open | + outbound HTTPS (brokers, Stripe, Cloudflare, LLM) |

Software: Docker Engine + Compose v2 and git:
```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER && newgrp docker
docker compose version   # must print v2.x
```

## 2. Get the code onto the server

1. In the Emergent chat, use **“Save to GitHub”** to push the current
   codebase to your GitHub repository (private recommended).
2. On the server:
```bash
git clone git@github.com:<you>/<repo>.git stoic && cd stoic
```

## 3. One-command install

```bash
deploy/install.sh --production stoicaibot.com
```
What it does (idempotent — safe to re-run):
- generates all Docker secrets in `./secrets/` (Mongo root+app passwords,
  `mongo_url`, `jwt_secret`, `key_vault_master`, `metrics_token`);
- creates `./.env` and `backend/.env` from the templates, enforcing
  `APP_ENV=production`, CSRF enforcement and `CORS_ORIGINS`;
- wires `COMPOSE_FILE=docker-compose.yml:docker-compose.tls.yml` so Caddy
  terminates TLS on 80/443 with automatic Let's Encrypt certificates;
- builds and starts: Mongo (authenticated, not exposed), backend API
  (loopback-only :8001), **six dedicated workers** (trading, protection,
  reconciliation, analytics, model, tuning), frontend nginx (loopback-only
  :3000), Caddy ingress;
- waits for `/api/health` and prints worker status.

> Let's Encrypt needs the domain to resolve to this server (step 6). Until
> then Caddy will retry certificate issuance automatically — everything else
> already works via `curl -H "Host: stoicaibot.com" http://127.0.0.1`.

## 4. Configure `backend/.env` (integrations + admin)

The installer generates the security-critical values. Add / carry over your
integration keys (see `.env.example` for every key):

| Key | Value / note |
|---|---|
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | the installer GENERATES a strong admin password — read it from `backend/.env` after install (or set your own: ≥12 chars, not `admin123`) |
| `ADMIN_MFA_ENFORCED` | `true` |
| `CORS_ORIGINS` | `https://stoicaibot.com,https://www.stoicaibot.com` (installer sets both from the domain; Caddy serves apex + www) |
| `TURNSTILE_SITE_KEY` / `TURNSTILE_SECRET_KEY` | same Cloudflare widget — hostnames unchanged, nothing to re-whitelist |
| `STRIPE_API_KEY` (+ webhook secret) | same account; webhook URL keeps working because the domain doesn't change |
| `EMERGENT_LLM_KEY` | works off-platform (billing stays on your Emergent account) — or swap in your own OpenAI/Anthropic keys |
| `RESEND_*` / `TELEGRAM_*` / `NEWSAPI_KEY` | as currently used |
| `WEBAUTHN_RP_ID` | `stoicaibot.com` (apex + www share admin passkeys) |
| `RELEASE_SIGNER_ALLOW_LOCAL_IN_PROD` | `true` until you attach a KMS/external signer |

Apply changes: `docker compose up -d` (recreates readers).

## 5. Data: fresh start or migrate

**Option A — fresh start (simplest).** First boot seeds the admin account,
brokers, knowledge base and indexes automatically. Users re-register; EAs
re-pair.

**Option B — migrate the Emergent production data.** You don't have direct
access to the managed Mongo, so email support@emergent.sh and request a
**mongodump archive of your production database** (app `stoic-trading`).
Then on your server:
```bash
# copy the dump to ./backups/ first
deploy/backup.sh restore backups/<dump-file>
```
The restore runs in maintenance mode (stops writes, validates the archive
with a dry run first) and restarts the API; bring workers back with
`docker compose up -d` after verifying.

## 6. DNS cutover (Cloudflare)

Current: `www.stoicaibot.com` → CNAME → Emergent. Change to your server:
1. Cloudflare DNS: set `A` records for `stoicaibot.com` and `www` to your
   server's IP. Keep the proxy (orange cloud) ON if you want Cloudflare in
   front — set SSL/TLS mode to **Full (strict)** once Caddy has its cert.
   (Tip: to let Let's Encrypt issue quickly, you can grey-cloud briefly,
   then re-enable the proxy.)
2. Wait for propagation (~minutes), then:
```bash
curl -fsS https://www.stoicaibot.com/api/health   # → {"status":"ok",...}
```
3. **MT5 EAs keep working automatically** — they call the same domain.
   No EA reinstall needed; heartbeats resume as soon as DNS flips.

Rollback at any time: point the DNS records back at the Emergent target.

## 7. Verify (from docs/DEPLOYMENT.md §Verification)

1. `docker compose ps` — mongo, backend, 6 workers, frontend, caddy all Up
2. Log in as admin → **Admin → Deploy Preflight** → verdict READY
3. **Admin → Ops Console** → Runtime Health card (uptime/RSS/loop-lag) and
   Execution Health all green
4. Trading Safety banner shows MODE / RECON / PANIC pills
5. EA on a demo terminal: heartbeat arrives, certification matrix green
6. `deploy/backup.sh backup` produces an archive; `deploy/backup.sh verify <file>` passes

## 8. Day-2 operations

| Task | Command |
|---|---|
| Update to latest main (auto-rollback on failure) | `deploy/update.sh` |
| Update to a signed release | `deploy/update.sh v1.4.0` |
| Nightly backups (14-day retention) | `deploy/backup.sh schedule` → add printed line to crontab |
| Off-site backups | set `BACKUP_RCLONE_REMOTE` or `BACKUP_S3_URI`, `BACKUP_OFFSITE=true` |
| Manual rollback | `deploy/rollback.sh` |
| Logs | `docker compose logs -f backend worker-trading` |
| Resource watch | `docker stats`; in-app: Ops Console → Runtime Health |
| Prometheus scrape | `GET /api/metrics` with `X-Metrics-Token: $(cat secrets/metrics_token)` |

## 9. Sizing notes for your bigger server

- The compose topology already splits the 6 worker loops out of the API
  process (`BACKGROUND_WORKERS_IN_PROCESS=false`), so no single container
  needs >1–2 GB.
- With ≥8 GB RAM you can enable in-API heavy ML: the gate auto-enables when
  the container sees ≥`ML_MIN_MEMORY_GB` (default 2 GB) — no config needed.
- Mongo will happily use spare RAM for cache; cap it if you prefer:
  add `--wiredTigerCacheSizeGB 2` to the mongo command in compose.

## 10. Decommissioning the Emergent deployment

After a week of stable soak on your server: keep the Emergent deployment as
a warm standby or unpublish it from the Emergent dashboard. Your Cloudflare
DNS is the switch between the two at any time.

---
Related: `docs/DEPLOYMENT.md`, `docs/RUNBOOK.md`, `docs/DISASTER_RECOVERY.md`,
`docs/ROLLBACK.md`, `docs/INCIDENT_RESPONSE.md`.
