# Host Migration — move a live STOIC install to a new server

The admin wizard at **/admin/host-migration** moves everything (MongoDB, `./secrets`,
`backend/.env`, `./.env`, `./backups`, release evidence, Caddy TLS certificates, the
exact checkout/release) to a new host with a freeze window of a few minutes. Same
domain → every EA keeps its bridge token and reconnects by itself once DNS moves.

## How it is wired (and why)

```
browser ──HTTPS──▶ API container ──http://migrator:8790──▶ migrator sidecar ──SSH/rsync──▶ NEW HOST
   (admin + 2FA step-up)   (proxy only, no docker/ssh)   (docker socket, own ssh key)
```

* The **API never gets docker or SSH access**. `docker-compose.migrator.yml` adds a
  sidecar (`Dockerfile.migrator`: docker CLI + compose + ssh + rsync + python) that
  mounts the docker socket and the checkout (same absolute path) and is reachable
  **only on the compose network**. The API authenticates to it with the shared
  `METRICS_TOKEN` Docker secret.
* Admin session **plus fresh 2FA step-up** for `start`, `freeze`, `decommission`, `abort`.
  Every wizard action is journaled in `host_migration_events`.
* The sidecar generates its **own ed25519 key** on first use; only the public key is
  shown. Target host key is pinned on preflight (`StrictHostKeyChecking=yes`).
* Enable only for the migration, remove afterwards:

```bash
make migrator-on      # appends docker-compose.migrator.yml, restarts API with MIGRATOR_URL
make migrator-off     # removes it again
MIGRATOR_DRY_RUN=1 make migrator-on   # REHEARSAL: whole wizard with simulated commands
```

## Prepare the new host (once)

```bash
# as root on the NEW host (AlmaLinux/Rocky/Ubuntu)
useradd -m stoic && usermod -aG docker stoic          # after installing docker:
curl -fsSL https://get.docker.com | sh && systemctl enable --now docker
dnf install -y rsync            # (apt install rsync on Debian/Ubuntu)
mkdir -p /home/stoic/.ssh && echo '<public key from the wizard>' >> /home/stoic/.ssh/authorized_keys
chown -R stoic: /home/stoic/.ssh && chmod 700 /home/stoic/.ssh && chmod 600 /home/stoic/.ssh/authorized_keys
```

Open ports 22, 80 and 443. Nothing else is needed — the wizard installs STOIC.

## The wizard, step by step

| # | Step | Source (old host) | New host | Gate |
|---|------|-------------------|----------|------|
| 1 | **Preflight** | reads release tag/SHA, profile (`--production <domain>`, forecast, registry), DB + backups size, connected accounts | SSH login, docker/compose/rsync, docker access, empty path, disk ≥ 3×DB+backups+6 GB, RAM, ports 80/443/8001/3000/27017 free, public IP | all checks green |
| 2 | **Install** | `rsync` checkout + secrets + env + backups + release evidence; streams Caddy cert volume | `deploy/install.sh` with the **same** flags; then `docker compose stop worker-*` (must not trade yet) | — |
| 3 | **Warm sync** | `mongodump` streamed over SSH | `mongorestore --drop` | admin types `FREEZE` |
| 4 | **Freeze** | `docker compose stop` API + 6 workers — **downtime starts** | — | — |
| 5 | **Final sync** | second dump (delta since warm sync; fast) | restore | — |
| 6 | **Verify** | — | `docker compose up -d`, wait for `/api/ops/release-readiness` READY, `/api/health` | admin moves DNS |
| 7 | **Cutover check** | resolves domain, `curl --resolve` against the new IP (TLS/cert), public health | counts accounts with `last_heartbeat` newer than the freeze → **EAs arrived** | ≥1 heartbeat (or 0 connected accounts) |
| 8 | **Decommission** | `docker compose stop` (everything; data kept on disk) | live | admin types `DECOMMISSION` |

**Abort** (any time before decommission): restarts the source stack if it was frozen and
stops the new host's workers again. **Retry** re-runs the failed step chain.

## Cutover with Cloudflare
Dashboard → DNS → edit the `A` record of your domain → new host's public IP, keep the
proxy (orange cloud) on. Propagation behind the proxy is near-instant; EAs retry every
few seconds and heartbeat to the new host, which the wizard detects.

## After the migration
1. On the **new** host: `make migrator-off` (if you enabled it there too), `make status`.
2. Bot Health → Execution Health all green; Verified Performance attestation fresh.
3. Keep the old server stopped for a few days as a rollback, then destroy it.
4. Update GitHub Actions secrets (`DEPLOY_HOST`) if you use tag-triggered publishing.

## Rollback after cutover
Point DNS back to the old IP and on the old host run `docker compose up -d`. Trades that
happened on the new host in between are NOT on the old host — export them first
(`deploy/backup.sh backup` on the new host, restore on the old one) if that matters.
