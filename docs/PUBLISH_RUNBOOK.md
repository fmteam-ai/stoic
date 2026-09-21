# STOIC — Publish Runbook for your AlmaLinux 8.10 server

**Target machine:** Xeon E-2134 (4C/8T) · 64 GB RAM · 2 TB SSD · 1 Gbps · AlmaLinux 8.10
**Domain:** `stoicaibot.com` (Cloudflare-proxied)
**Time budget:** ~2 hours first publish · **~3 minutes per update afterwards**

This is the step-by-step version. Background and alternatives live in
`docs/SELF_HOSTING_GUIDE.md`, `docs/DEPLOYMENT.md`, `docs/RUNBOOK.md`,
`docs/DISASTER_RECOVERY.md`.

---

## Roadmap at a glance

| Phase | What | Outcome | Est. |
|---|---|---|---|
| 0 | Get the code into your GitHub | private repo = single source of truth | 5 min |
| 1 | Prepare the server (Docker, firewall, SELinux, user) | clean Docker host | 20 min |
| 2 | Clone + one-command install (`--production --with-forecast`) | full stack running behind Caddy TLS on the server IP | 20 min |
| 3 | Configure integrations (`backend/.env`) + secrets carry-over | Stripe, Resend, Telegram, Turnstile, LLM key live | 15 min |
| 4 | Data migration (or fresh start) + verification | Mongo restored, readiness green, 6 workers leased | 20 min |
| 5 | DNS cutover on Cloudflare + first-login checks | `https://stoicaibot.com` served from your box | 15 min |
| 6 | Day-2: backups, monitoring, update flow, rollback drill | operable, recoverable | 20 min |
| 7 | Promotion: shadow → demo → supervised small live | autonomous live trading unlocked by evidence | days |

---

## Phase 0 — Code into GitHub (5 min)

1. In this Emergent chat, click **Save to GitHub** → create a **private** repo (e.g. `yourname/stoic`).
2. Every later change you make here is pushed the same way; the server pulls from that repo.
3. (Optional, for tagging releases) In GitHub → Releases → *Draft a new release* → tag `v1.0.0`. Tags are what you will publish to production.

> Rule of thumb: **`main` = tested candidate, `vX.Y.Z` tag = what runs in production.**

---

## Phase 1 — Prepare the server (20 min)

SSH in as root (or a sudoer) and run:

```bash
# 1a. base tooling + Docker CE (AlmaLinux 8 uses the CentOS Docker repo)
sudo dnf -y install dnf-utils git curl python3 policycoreutils-python-utils
sudo dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
sudo dnf -y install docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo systemctl enable --now docker

# 1b. dedicated deploy user (owns the checkout, in the docker group, no root needed for updates)
sudo useradd -m -s /bin/bash stoic
sudo usermod -aG docker stoic

# 1c. firewall: only 22, 80, 443 reach the box
sudo firewall-cmd --permanent --add-service=ssh --add-service=http --add-service=https
sudo firewall-cmd --reload

# 1d. SELinux stays ENFORCING — allow containers to use bind-mounted files
sudo setsebool -P container_manage_cgroup on

# 1e. sanity
docker --version && docker compose version
```

Optional but recommended: harden SSH (key-only, no root login) and set the timezone `sudo timedatectl set-timezone UTC` (trading logs are UTC).

---

## Phase 2 — Clone + install (20 min)

As the `stoic` user:

```bash
sudo -iu stoic
git clone git@github.com:yourname/stoic.git /home/stoic/stoic   # or https:// with a PAT
cd /home/stoic/stoic

# Forecast profile ON — your 64 GB box runs the Chronos forecaster + GBM ensemble.
deploy/install.sh --production stoicaibot.com --with-forecast
```

What the installer does (all idempotent, safe to re-run):

- generates Docker **secrets** in `./secrets/` (Mongo root/app passwords, `mongo_url`, `jwt_secret`, `key_vault_master`, `metrics_token`) — never in `.env`
- writes `./.env` (compose config) and `backend/.env` (non-secret app config) from templates
- builds images **with build provenance** (`GIT_SHA` → `/api/health.build_sha`, image digest → `STOIC_IMAGE_DIGEST`)
- starts Mongo (authenticated, loopback), API, **6 workers**, frontend, **Caddy** (auto Let's Encrypt on 80/443)
- verifies API health, frontend, **full release readiness** (worker leases, reconciliation, outbox, schema) and the forecast plane
- prints the generated admin password — **save it in your password manager now**

If SELinux blocks a bind mount you will see `permission denied` in `docker compose logs`; fix once with:

```bash
sudo chcon -Rt container_file_t /home/stoic/stoic/deploy /home/stoic/stoic/secrets
```

> Caddy cannot get a certificate until DNS points at the box (Phase 5). Until then the installer's HTTPS check may warn — that is expected; everything else must be green.

---

## Phase 3 — Integrations & secrets carry-over (15 min)

Edit `backend/.env` (non-secret config; secrets that *must* stay secret go in `./secrets/` and are read via `*_FILE`):

| Key | Where to get it | Notes |
|---|---|---|
| `STRIPE_API_KEY`, `STRIPE_WEBHOOK_SECRET` | Stripe dashboard → Developers | point the webhook to `https://stoicaibot.com/api/webhook/stripe` |
| `RESEND_API_KEY`, `SENDER_EMAIL` | resend.com | verify the sending domain |
| Telegram | *(no env)* — each user pastes their bot token + chat id in **Notification settings**; stored encrypted in the key vault | that is why `KEY_VAULT_MASTER` must be carried over |
| `TURNSTILE_SITE_KEY`, `TURNSTILE_SECRET_KEY` | Cloudflare → Turnstile | same widget works — hostname unchanged |
| `EMERGENT_LLM_KEY` | Emergent → Profile → Universal Key | works off-platform (billed to your Emergent balance) or swap in your own OpenAI/Anthropic keys |
| `NEWSAPI_KEY`, `FRED_API_KEY`, `ALPHA_VANTAGE_KEY`, `FINNHUB_KEY` | providers | macro/news feeds |
| `APP_ENV=production`, `ADMIN_MFA_ENFORCED=true`, `WEBAUTHN_RP_ID=stoicaibot.com`, `TRUST_CF_CONNECTING_IP=true`, `CORS_ORIGINS=https://stoicaibot.com` | — | production posture (the server refuses to boot in production with MFA enforcement off) |

**Critical carry-over from the current Emergent production:** request the production **`KEY_VAULT_MASTER`** from support@emergent.sh and write it to `secrets/key_vault_master` **before** restoring data. It decrypts stored broker credentials; without it users must re-enter them once.

Apply: `docker compose up -d` (env changes only need a restart, not a rebuild).

---

## Phase 4 — Data + verification (20 min)

**Fresh start:** nothing to do — the admin account was seeded at install.

**Migrate from Emergent:** request the production Mongo dump from support, copy it to the server, then:

```bash
deploy/backup.sh verify  /path/prod-dump.archive   # restores into a throwaway Mongo and compares counts
deploy/backup.sh restore /path/prod-dump.archive   # maintenance-mode restore into the live Mongo
```

Verify the whole topology (this is exactly what `update.sh` checks on every publish):

```bash
make status                         # containers · running build_sha · last releases
curl -s http://127.0.0.1:8001/api/health | python3 -m json.tool
curl -s -H "X-Metrics-Token: $(cat secrets/metrics_token)" http://127.0.0.1:8001/api/ops/release-readiness | python3 -m json.tool
docker compose exec worker-trading python ops/verify_forecast_profile.py   # forecast plane READY
```

Green means: `status: ok`, 6 worker leases held, reconciliation fresh, outbox empty, schema compatible.

---

## Phase 5 — DNS cutover on Cloudflare (15 min)

1. Cloudflare → DNS → `stoicaibot.com` **A record → your server IP**, proxy status **Proxied** (orange cloud) — keep it proxied so Turnstile, rate-limit IP trust (`TRUST_CF_CONNECTING_IP`) and DDoS protection continue to work.
2. Cloudflare → SSL/TLS → **Full (strict)** (Caddy presents a valid Let's Encrypt cert).
3. Within a minute Caddy provisions the certificate: `docker compose logs caddy --tail 20` shows `certificate obtained`.
4. First-login checklist at `https://stoicaibot.com`:
   - log in as admin → enrol TOTP/passkey (MFA is enforced)
   - **Bot Health** → Readiness, Authority strip, **Forecast Health = RUNNING**
   - **Accounts** → an EA heartbeat arrives once a terminal points at the new host (the EA URL is the domain, so nothing changes for users)
   - Stripe → send a test webhook from the dashboard → 200
5. Rollback of the cutover = flip the A record back. Keep the Emergent deployment alive for 48 h, then decommission (guide §10).

---

## Phase 6 — Day-2 operations (20 min)

```bash
deploy/backup.sh schedule     # prints crontab lines: nightly backup + automated restore-verify
crontab -e                    # paste them; set BACKUP_PASSPHRASE_FILE / BACKUP_OFFSITE for encrypted off-site copies
```

- **Logs:** `docker compose logs -f backend worker-trading`
- **Alerts:** Notification settings → Telegram/email; Bot Health auto-resolves alerts when conditions clear.
- **Resource view:** `docker stats` — the forecast profile pins memory/CPU per container so nothing can OOM a neighbour.
- **Security scans** run in GitHub CI on every push (`security-scan`, `container-build`); do not publish a red build.

---

## Publishing updates — the easy way

Everything funnels into **one command on the server**: `deploy/update.sh` (alias `make publish`). It always does, in order: **backup → fetch → release-attestation gate → rebuild with provenance → restart → verify API + running SHA + frontend + full readiness (+ forecast plane) → auto-rollback on any failure → append to `deploy/releases.log`**. Only one publish can run at a time (lock).

### The release-attestation gate (what "signed release" means here)

Every `vX.Y.Z` tag makes CI (`release.yml`) emit **`release-attestation.json`** — commit SHA, GHCR image digests, exact test counts (unit + integration junit), `pip-audit` and `grype` results, every gate (tests, dependency audit, image scan, clean-install readiness, EA compile) and a `promotion_decision`. CI signs it **keyless with cosign/Sigstore** (identity = this repo's workflow) and attaches it, its `.sig`/`.pem`, and the raw evidence files to the GitHub Release.

On the server, `deploy/update.sh` and a first `--production` install **refuse to build** unless:

1. the commit carries a `v*` tag (branches are never deployable to production),
2. the attestation for that tag downloads from the GitHub Release (`GITHUB_TOKEN` in `./.env` for private repos),
3. `cosign verify-blob` proves it was signed by **your** repo's GitHub workflow,
4. the content gate passes: SHA matches, 0 failed/errored tests, ≥ 500 tests, 0 pip-audit vulns, 0 fixable critical image vulns, all gates green, decision `APPROVED`.

The verified record is kept at `release/attestation.current.json`. `ATTESTATION_REQUIRED=false` in `./.env` disables the gate (dev only). cosign is installed automatically on first use.

### Option A — two clicks + one command (recommended to start)

1. In Emergent: build/fix → **Save to GitHub** (pushes `main`).
2. On the server:
   ```bash
   sudo -iu stoic; cd ~/stoic
   make publish                 # = deploy/update.sh origin/main
   # or pin a release:  make publish REF=v1.4.2
   ```
3. Read the last line: `== update complete ==` — or the auto-rollback notice with the reason.

### Option B — zero-touch from GitHub (tag → live)

`.github/workflows/deploy-production.yml` SSHes into your server and runs the same `deploy/update.sh` whenever you **push a `v*` tag** (or from *Actions → deploy-production → Run workflow* with any ref).

One-time setup:

```bash
# on the server, as stoic: a dedicated deploy key
ssh-keygen -t ed25519 -f ~/.ssh/github_deploy -N "" -C "github-deploy"
cat ~/.ssh/github_deploy.pub >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
cat ~/.ssh/github_deploy          # ← private key → GitHub secret DEPLOY_SSH_KEY
```

GitHub → repo → Settings → Secrets and variables → Actions:

| Secret | Value |
|---|---|
| `DEPLOY_HOST` | server IP |
| `DEPLOY_USER` | `stoic` |
| `DEPLOY_PATH` | `/home/stoic/stoic` |
| `DEPLOY_SSH_KEY` | the private key above |
| `DEPLOY_PORT` | `22` (optional) |

And in the server's `./.env` (for a **private** repo, so the attestation can be downloaded): `GITHUB_TOKEN=<fine-grained PAT, Contents: read-only>` and optionally `GITHUB_REPO=yourname/stoic`.

Then publishing is: GitHub → Releases → new tag `v1.4.2` → the workflow deploys, verifies, and auto-rolls back if anything fails. The Actions log shows `deploy/releases.log` and container status at the end.

> Tip: protect the `production` environment in GitHub (Settings → Environments) with a required reviewer if you want a manual approval click before anything reaches the server.

### Option C — registry image deploys (no rebuild on the server)

By default the server **rebuilds** the images from the checkout. In **registry mode** it instead pulls the *exact* images CI built, signed and recorded in the attestation — byte-for-byte what passed the test/scan gates, and no build CPU/RAM on the server.

```bash
# fresh install
deploy/install.sh --production trade.example.com --registry
# or switch an existing server
echo "DEPLOY_MODE=registry" >> .env   # (set_kv semantics — one line, no duplicates)
make publish REF=v1.4.2
```

What `provision_images` does in registry mode (`deploy/lib.sh::pull_attested_images`):

1. the release-attestation gate is **always** required (it is the only source of the digests — `ATTESTATION_REQUIRED` is ignored),
2. reads `images.backend` / `images.frontend` from the verified `release/attestation.current.json` and refuses anything that is not `…@sha256:<64 hex>`,
3. `cosign verify` on **each image digest** (keyless, identity pinned to `https://github.com/<you>/<repo>/` workflows),
4. `docker pull` **by digest**, then checks the pulled `RepoDigests` equals the attested one,
5. pins `STOIC_BACKEND_IMAGE` / `STOIC_FRONTEND_IMAGE` / `STOIC_IMAGE_DIGEST` / `GIT_SHA` into `./.env` and adds `docker-compose.registry.yml` to `COMPOSE_FILE` (`pull_policy: never` — compose can only run what the gate pulled),
6. `docker compose up -d --no-build` — a local build can never sneak in.

Private GHCR packages: put `GITHUB_TOKEN=<PAT with read:packages + Contents: read>` in `./.env`; `registry_login` uses it for `docker login ghcr.io`. Public packages need nothing. Rollback (`deploy/rollback.sh` / auto-rollback) re-verifies and re-pulls the previous release's digests the same way. Switch back any time with `DEPLOY_MODE=build`.

### Staging acceptance drills (before promoting a release)

`make staging-acceptance` runs AT-01 (six-account execution boundary, no downtime); `make staging-acceptance ROLLBACK=1` adds AT-15 (signed-release verification + forced readiness failure → automatic rollback, causes a few minutes of downtime — staging only). Evidence lands in `release/drills/`. See `docs/RELEASE_ACCEPTANCE_CHECKLIST.md`.

### Moving to another server

Use the admin wizard **/admin/host-migration** (enable the sidecar first with `make migrator-on`). It installs the same release on the new host, carries secrets/env/backups/TLS certs, warm-syncs the database while the old host keeps trading, then freezes, final-syncs, verifies and waits for EA heartbeats on the new host before you decommission the old one. Details: `docs/HOST_MIGRATION.md`.

### Rolling back

```bash
make rollback                 # previous verified release from deploy/releases.log
make rollback REF=v1.4.1      # explicit tag/sha
deploy/rollback.sh v1.4.1 --with-db backups/<archive>   # also restore the DB snapshot taken before that update
```

### What changes need what

| You changed | Server action |
|---|---|
| any code (backend/frontend/EA) | `make publish` (rebuild is automatic) |
| `backend/.env` value | `docker compose up -d` |
| a Docker secret in `./secrets/` | `docker compose up -d --force-recreate backend worker-*` |
| forecast profile on/off | edit `COMPOSE_FILE` in `./.env`, then `make publish` |

---

## Phase 7 — Promotion gates before autonomous live

Do not go straight to autonomous live on the new host. The platform's own gates walk you through it:

1. **Shadow** — bots enabled, `mode=paper`, 48 h; Bot Health ≥ 90, zero UNKNOWN executions.
2. **Demo** — real broker demo accounts, EA v1.56 connected, 14-day Soak Tracker green, position truth FRESH throughout.
3. **Supervised small live** — one LIVE account, minimum lots, Risk Commander confirmations on, daily reconciliation review.
4. **Autonomous live** — only when the Certification and Release Readiness cards are fully green and the Verified Performance attestation is issued (LIVE-classified, reconciled, fresh).

---

## Quick reference

```bash
make status                       # health, running SHA, containers, last releases
make publish [REF=vX.Y.Z]         # re-publish with full verification + auto-rollback
make rollback [REF=...]           # previous good release
make backup                       # ad-hoc Mongo backup
docker compose logs -f backend    # live API log
docker compose exec worker-trading python ops/verify_forecast_profile.py
```
