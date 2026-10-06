# Production Deploy Checklist — why "Deployment failed" and how to fix it

**Symptom.** The Emergent deploy of stoicaibot.com fails its health check; the
live site keeps serving the previous build (`/api/health` 500, new routes 404).

**Root cause.** With `APP_ENV=production` the backend REFUSES TO BOOT
(`server.py::on_startup`, `release_signing.signer_config_violations`) when the
environment still carries preview-only values. Production inherits
`backend/.env` from the codebase; anything not overridden in the deploy
panel's **Secrets / Environment** tab reaches production as-is.

Reproduce the exact list locally at any time:

```bash
python scripts/production_preflight.py        # exit 1 while verdict=will_crash
```

## Current blockers (5)

| # | Variable | Production inherits | Must be |
|---|----------|--------------------|---------|
| 1 | `STEP_UP_BYPASS_TOKEN` | set | **empty / removed** |
| 2 | `RATE_LIMIT_BYPASS_TOKEN` | set | **empty / removed** |
| 3 | `ADMIN_MFA_ENFORCED` | `false` | **`true`** — enrol TOTP on the admin BEFORE deploying (Settings → Two-factor) or admin pages are locked |
| 4 | `RELEASE_SIGNER` | `local` | **`external`** + `RELEASE_SIGNER_URL`, `RELEASE_SIGNER_TOKEN`, `RELEASE_SIGNER_ALLOWED_HOSTS`, `RELEASE_SIGNER_KEY_ID`, `RELEASE_PUBLIC_KEY_B64`, `RELEASE_SIGNER_TIMEOUT` |
| 5 | `ED25519_SIGNING_KEY_B64` | present | **removed** from the API env (lives only in the signer) |

Everything else already passes (CORS origins, dedicated `ORDER_AUTH_SECRET` /
`LEDGER_ANCHOR_KEY`, statement attestation key, strong admin password).

## Step 1 — the isolated release signer (blocker 4 + 5)

The signing key must never live inside the trading API. Host the bundled
signer (`deploy/signer/`) on infrastructure separate from the API. It cannot
be hosted from the Emergent preview workspace (no Docker, not isolated).

### Fastest path — Fly.io (one command, free tier, automatic HTTPS)
```bash
curl -L https://fly.io/install.sh | sh && flyctl auth login   # once
pip install cryptography                                      # once
cd deploy/signer && ./deploy_fly.sh stoic-signer-<yourname> ams
```
The script generates the keypair + bearer token locally, stores them ONLY as
Fly secrets (+ chmod-600 copies in `~/.stoic-signer/`), deploys, verifies
`/healthz`, `/public-key` and the authenticated `/health`, and prints the
complete **Secrets-tab block** for Step 2 plus the signer host URL
(`https://<app>.fly.dev`). Re-verify from anywhere with
`python scripts/signer_probe.py --url https://<app>.fly.dev --token-file ~/.stoic-signer/<app>.token --public-key <PUBLIC>`
(sign → verify round-trip through the same client code the API uses).

### Manual path (any VPS / container platform)

1. Generate a fresh keypair (anywhere with `pip install cryptography`):
   ```bash
   python - <<'EOF'
   import base64
   from cryptography.hazmat.primitives import serialization as s
   from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
   k = Ed25519PrivateKey.generate()
   print("PRIVATE (signer only):", base64.b64encode(k.private_bytes(s.Encoding.Raw, s.PrivateFormat.Raw, s.NoEncryption())).decode())
   print("PUBLIC  (API pin)    :", base64.b64encode(k.public_key().public_bytes(s.Encoding.Raw, s.PublicFormat.Raw)).decode())
   EOF
   openssl rand -hex 32   # → SIGNER_TOKEN
   ```
2. Run the signer behind HTTPS (terminate TLS with Caddy/nginx or the platform):
   ```bash
   cd deploy/signer
   docker build -t stoic-signer .
   docker run -d --name stoic-signer --restart unless-stopped -p 9443:9443 \
     -e SIGNER_TOKEN="<token from step 1>" \
     -e ED25519_SIGNING_KEY_B64="<PRIVATE from step 1>" \
     stoic-signer
   curl -s https://<signer-host>/healthz      # {"status":"ok"}
   curl -s https://<signer-host>/public-key   # must equal the PUBLIC key
   ```
   Key id is fixed by the service: `stoic-release-ed25519-v1`.

## Step 2 — Secrets tab values for the API deploy

```
APP_ENV=production
ADMIN_MFA_ENFORCED=true
STEP_UP_BYPASS_TOKEN=            (empty — or delete the key)
RATE_LIMIT_BYPASS_TOKEN=         (empty — or delete the key)
ED25519_SIGNING_KEY_B64=         (empty — or delete the key)
RELEASE_SIGNER=external
RELEASE_SIGNER_URL=https://<signer-host>
RELEASE_SIGNER_ALLOWED_HOSTS=<signer-host>
RELEASE_SIGNER_TOKEN=<SIGNER_TOKEN>
RELEASE_SIGNER_KEY_ID=stoic-release-ed25519-v1
RELEASE_PUBLIC_KEY_B64=<PUBLIC from step 1>
RELEASE_SIGNER_TIMEOUT=10
```

Keep the existing production-only values already in place (`CORS_ORIGINS`
with `https://stoicaibot.com,https://www.stoicaibot.com`, `JWT_SECRET`,
`ORDER_AUTH_SECRET`, `LEDGER_ANCHOR_KEY`, `STATEMENT_ATTESTATION_PUBLIC_KEY_B64`,
`STRIPE_API_KEY` (rotated live key), `RESEND_API_KEY` + verified `SENDER_EMAIL`
domain, `TURNSTILE_*` if the Turnstile policy is enabled, `BACKGROUND_WORKERS_IN_PROCESS`).

## Step 3 — verify after the deploy

One command (PASS only on the audited production build; old build ⇒ "still serves the OLD build"):
```bash
python scripts/live_probe.py --expect-sha <deployed commit>
```
Equivalent manual curls:
```bash
curl -s https://www.stoicaibot.com/api/health           # build_sha == deployed commit, app_env=production, env_sig present
curl -s https://www.stoicaibot.com/api/status | jq .trading.attestation   # R15 availability-only attestation block
curl -s -o /dev/null -w '%{http_code}\n' https://www.stoicaibot.com/api/authority/decision   # 401 (route exists; auth required — old build: 404)
curl -s -o /dev/null -w '%{http_code}\n' https://www.stoicaibot.com/api/ledger/statements    # 401 (route exists — old build: 404)
```
Then log in as admin (TOTP required) and open **/admin/runbooks → Deploy
preflight** (`GET /api/ops/deploy-preflight`) — every row must be `pass`.

## MongoDB transactions are REQUIRED in production (audit r17 P0-01)
Fenced Risk Commander / trigger effects commit the effect-row assertion, the
domain mutation and the `completed` transition in ONE transaction. On a
standalone `mongod` the API **fails closed** (the action is recorded `failed`,
nothing is written) and the preflight row `mongo_transactions` is FAIL.
Transactions are required whenever capital can be touched — production, ANY
live-mode account with trading enabled, regardless of APP_ENV (r18 P1-01). The
non-atomic standalone fallback exists only for `NL_EFFECTS_SYNTHETIC_ONLY=true`
with zero live-enabled accounts (CI). **The self-host stack does this for you**:
`docker-compose.yml` runs `mongo` as a single-node replica set (`rs0`, cluster
keyFile from `secrets/mongo_keyfile`, generated by `deploy/install.sh`), the
container healthcheck runs `rs.initiate()` once and is green only when
PRIMARY, and `secrets/mongo_url` carries `replicaSet=rs0`. Verify with
`deploy/doctor.sh --db` (row `replica set: rs0 (transactions available)`).
For a MongoDB you run yourself, the manual equivalent is:

```bash
openssl rand -base64 756 > secrets/mongo_keyfile && chmod 400 secrets/mongo_keyfile   # owned by the mongod uid (999)
# mongod --bind_ip_all --replSet rs0 --keyFile <keyfile>   (what deploy/mongo-start.sh does)
mongosh -u "$MONGO_ROOT_USER" -p "$(cat secrets/mongo_root_password)" \
  --eval 'try { rs.status() } catch (e) { rs.initiate({_id:"rs0", members:[{_id:0, host:"mongo:27017"}]}) }'
```
`MONGO_URL` gains `&replicaSet=rs0`. Managed MongoDB (Atlas etc.) already qualifies.


## main93 / A9 — before and after `deploy/update.sh` (2026-10-05)
Before:
- `cd /opt/stoic && (umask 077; : > secrets/security_telegram_token)` (or paste the BotFather token); remove
  `SECURITY_AGENT_TELEGRAM_BOT_TOKEN` from `backend/.env` (the worker reads the Docker secret). `update.sh` now creates
  the empty file itself (N-R2) — this is only needed if you paste the real token.
- `TRUSTED_PROXY_CIDRS` is defaulted by `update.sh` to the docker ranges (N-R1); with `TRUST_CF_CONNECTING_IP=true`
  every user now resolves to their own IP. Verify after deploy by logging in from two networks (Settings → Sessions).
- `EA_RELEASE_SHA256=<current 1.57 EX5 hash>` — until the signed 1.59 record exists live accounts are CLOSE_ONLY
  without it. After 1.59 is signed, move the 1.57 hash to `EA_RELEASE_SHA256_PREVIOUS` for the rollout window.
- `BRIDGE_TOKEN_HASH_KEY` — **REQUIRED** (32+ random chars, different from `JWT_SECRET`). Production REFUSES TO BOOT
  without it (A13-2; `deploy_preflight` check `bridge_hash_key` catches it first). Bridge tokens are stored only as
  HMAC hashes under this key. Set it ONCE before the first boot of this release and never change it: a later change
  invalidates every paired EA until re-paired. N97-4 — EAs paired under an EARLIER build (hashes made with the
  `JWT_SECRET` fallback) keep working: the first heartbeat after the key is set re-hashes the token under the new key
  (one-release dual-verify). Re-pair only if a terminal still shows "Invalid bridge token" after the deploy.
- Keep the security agent in `observe` with no rules enabled; `RESEND_API_KEY`, `WEBAUTHN_*`, admin 2FA as in main92.
After:
- `docker compose logs worker-security` shows one lease holder (the API log says "standing by" when the worker owns it).
- Admin → Bot Health → Release Identity shows BUILD SHA / IMAGE DIGEST / EA 1.60 / accepted EX5 hashes.
- Every user must sign in again once (access tokens are now revocable; pre-upgrade tokens are refused).
- Confirm P3 (1 trade per symbol per day) in Bot Pulse; retrain ML models.

## EA v1.60 (main98 N98-6) — rebuild required
EA 1.60 adds `trade_mode` (ACCOUNT_TRADE_MODE → `demo` | `real` | `contest`) to every heartbeat so
the server trusts a DEMO classification only when the **broker** says so:
- `demo` on an authoritative identity chain is positive DEMO evidence (`broker_reports_demo`) — the
  demo-named-server heuristic and the audited admin override are no longer needed for the proof.
- `real` / `contest` from ANY heartbeat is fail-closed: `broker_environment()` and
  `attested_environment()` return LIVE, an existing DEMO attestation shows INVALIDATED (LIVE gates —
  signed EX5 proof, close-only — apply immediately), a critical `demo_attestation_contradicted` alert
  is raised and the event is written to the hash-chained admin audit log.
- Terminals on 1.57–1.59 report nothing: the legacy path (server-name heuristic + audited override)
  stays valid for the 4-week demo; Demo Readiness shows `fleet_broker_demo` as WARN until 1.60 is
  rolled to every demo terminal (FAIL if any terminal reports real money).
Rollout: push to main → `ea-release.yml` signs the 1.60 EX5; keep the previous hash in
`EA_RELEASE_SHA256_PREVIOUS` during the window (same N-R6 dual-hash rule as below).

### ✅ 1.60 SIGNED — 2026-10-06 (ea-release run on commit `23943e2`, record commit `b7fa7d9`)
Public facts (no secrets) — `release/ea_release.json` is the source of truth:
- EX5 sha256 is recorded in `release/ea_release.json` (signed). **Do NOT set `EA_RELEASE_SHA256`** —
  the signed record IS the pin; an env pin is only for emergency overrides and never needed while a
  signed record exists (N100-6). `EA_RELEASE_SHA256_PREVIOUS` alone is allowed during a terminal rollout.
- MQ5 sha256 `897d6bf8768862b55e1432f897fc523dd0938431e8e8661f77cc16f7e20edc12` (matches shipped source)
- Compile: 0 errors / 0 warnings, Windows 10.0.26100, `compiled_by: github-actions`
- Signer: `https://stoic-signer.fly.dev` (Fly app `stoic-signer`, region ams), key id
  `stoic-release-ed25519-v1`, public key `/lYiSnGAY8/nWkdSKoGSXUJtDm0Syd+ioKTFa+s7cCo=`
  (`RELEASE_PUBLIC_KEY_B64`). Signature re-verified out-of-band against this key.
- `previous` is null (first signed release) — terminals still on ≤1.59 are CLOSE_ONLY on LIVE
  accounts until updated; add their hash to `EA_RELEASE_SHA256_PREVIOUS` only if a rollout window is needed.
- Token hygiene: the signer token was rotated right after the first signed run; GitHub secret
  `RELEASE_SIGNER_TOKEN` and the production API secret must carry the CURRENT value from
  `/root/.stoic-signer/stoic-signer.token` on the operator host (never in the repo or chat).

## EA v1.59 (main92 H1 / main93 decision 1) — rebuild required
EA 1.59 (the `margin_mode` change, renamed from the interim 1.58 as agreed — no signed 1.58 EX5
was ever produced) adds `margin_mode` (ACCOUNT_MARGIN_MODE) to every heartbeat so netting accounts
are recognised from the terminal itself. `docs/RELEASE_HASHES.json#ea.mq5_sha256` is captured for
the 1.59 source. Push to main → `ea-release.yml` compiles with MetaEditor, records + signs the EX5
(needs the RELEASE_SIGNER_* secrets) and commits `release/ea_release.json` back; then roll 1.59 to
every terminal. Terminals on 1.57 keep working (live minimum stays 1.57; `margin_mode_v1` is optional).

**N-R6 rollout rule (dual hash):** the backend accepts the CURRENT and the PREVIOUS signed EX5 hash
(`ea_release.json#previous`, written by `verify_ea_release.py --record` when the version changes) plus
the env pins `EA_RELEASE_SHA256` / `EA_RELEASE_SHA256_PREVIOUS`. Until the signed 1.59 record exists,
**set `EA_RELEASE_SHA256` to the 1.57 EX5 hash on the server** or live accounts go CLOSE_ONLY
(`EA_RELEASE_HASH_UNPINNED`). Once 1.59 is signed, keep the 1.57 hash in `EA_RELEASE_SHA256_PREVIOUS`
until every terminal is upgraded; remove it afterwards so 1.57 terminals are refused again.

## EA v1.57 (audit r17 P0-01) — rebuild required
`backend/static/EmergentTradingBridge.mq5` now parses `close_idem_key` /
`close_seq` from the bridge poll and durably (terminal Global Variables)
refuses a duplicate key or a lower per-trade `close_seq` BEFORE `OrderSend`
(`close_seq` is a backend-owned sequence monotonic across proposals, PANIC and
recovery — audit r18 P0-01). Live activation and canonical authority refuse
terminals below v1.57 or with an unverified/mismatched EX5 hash (r18 P0-02). Compile v1.57 in
MetaEditor, record the EX5 hash with `scripts/verify_ea_release.py --record`
(→ `release/ea_release.json`, or pin `EA_RELEASE_SHA256=<64 hex>` in the API
Secrets), and roll the terminals — the server reports `LATEST_EA = 1.57`.
**Binary proof is mandatory for live accounts (audit r20 P1-01):** canonical
authority is CLOSE_ONLY with `EA_RELEASE_HASH_UNPINNED` until the hash is
pinned, `EA_BINARY_PROOF_MISSING` until the terminal reports `ea_binary_sha256`
on its heartbeat, and `EA_BINARY_HASH_MISMATCH` when they differ.

## Not boot-blocking, but needed for the operational tooling
`STOIC_INSTALLATION_ID`, `RECONCILE_EXPECT=6/3/3`, `RECONCILE_APPROVED_POLICY`,
`RECONCILE_SCOPE_USER_ID` are read by `ops/production_reconcile.py` and the
policy-migration flow — set them once the API is up.
