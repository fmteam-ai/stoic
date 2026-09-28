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

## Not boot-blocking, but needed for the operational tooling
`STOIC_INSTALLATION_ID`, `RECONCILE_EXPECT=6/3/3`, `RECONCILE_APPROVED_POLICY`,
`RECONCILE_SCOPE_USER_ID` are read by `ops/production_reconcile.py` and the
policy-migration flow — set them once the API is up.
