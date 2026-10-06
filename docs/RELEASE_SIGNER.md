# Fixing the RELEASE_SIGNER preflight failure

## What the failure means
Your production env still has **`RELEASE_SIGNER_ALLOW_LOCAL_IN_PROD`** set, or
`RELEASE_SIGNER=local` / `ED25519_SIGNING_KEY_B64` is present.
That override was **removed in v56** — a set value is now treated as a
misconfiguration and turns the preflight check from *warn* into **fail**.

Since audit round 9 (P1-01) this **BLOCKS BOOT**: with `APP_ENV=production`
the API refuses to start (`release_signing.signer_config_violations`) until
an external signer is fully configured and the private key is absent from the
API environment — a failed deploy health check is the visible symptom.

## Step 1 — clear the fail immediately
In the production deployment environment variables:
- **DELETE** `RELEASE_SIGNER_ALLOW_LOCAL_IN_PROD` (whatever its value).

The check stays **fail** until `RELEASE_SIGNER=external` is configured — local signing in production is refused at preflight, at boot and at signing time by one shared validator (`release_signing.production_signer_violation`). There is no supervised-pilot bypass.
Boot is unaffected.

## Step 2 — reach PASS with the external signer
Host the bundled isolated signer (`deploy/signer/`) on infrastructure
SEPARATE from the API (small VPS, private network, or container platform):

```bash
cd deploy/signer
docker build -t stoic-signer .
docker run -d --name stoic-signer -p 9443:9443 \
  -e SIGNER_TOKEN="$(openssl rand -hex 32)" \
  -e ED25519_SIGNING_KEY_B64="<base64 raw 32-byte private key>" \
  stoic-signer
```

Generate a fresh keypair (run anywhere with python + cryptography):

```bash
python -c "import base64; from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey; from cryptography.hazmat.primitives import serialization as s; k=Ed25519PrivateKey.generate(); print('PRIVATE:', base64.b64encode(k.private_bytes(s.Encoding.Raw, s.PrivateFormat.Raw, s.NoEncryption())).decode()); print('PUBLIC :', base64.b64encode(k.public_key().public_bytes(s.Encoding.Raw, s.PublicFormat.Raw)).decode())"
```

Then set in the PRODUCTION env of the API (and remove the local key):

| Variable | Value |
|----------|-------|
| `RELEASE_SIGNER` | `external` |
| `RELEASE_SIGNER_URL` | `https://<signer-host>:9443` |
| `RELEASE_SIGNER_TOKEN` | the `SIGNER_TOKEN` you generated |
| `RELEASE_PUBLIC_KEY_B64` | the PUBLIC key (pins verification) |
| `ED25519_SIGNING_KEY_B64` | **remove** from the API env — the key now lives only in the signer |

The API verifies every returned signature against the pinned public key
(defence-in-depth) — a compromised signer cannot forge accepted output.

## GitHub Actions secrets (ea-release.yml / release.yml)
The EA compile-record-sign job signs through the SAME external signer. Add these as
repository secrets (Settings → Secrets and variables → Actions), otherwise the job stops
at *"Signer secrets present"* right after a successful compile:

| Secret | Value |
|--------|-------|
| `RELEASE_SIGNER_URL` | `https://<signer-host>:9443` (reachable from GitHub runners) |
| `RELEASE_SIGNER_TOKEN` | the `SIGNER_TOKEN` |
| `RELEASE_SIGNER_ALLOWED_HOSTS` | `<signer-host>` (comma list; the URL host must be in it) |
| `RELEASE_PUBLIC_KEY_B64` | the PUBLIC key |
| `RELEASE_SIGNER_KEY_ID` | key id the signer serves (e.g. `stoic-ea-2026`) |

Re-run: Actions → **ea-release** → *Run workflow* (ref `main`). The job commits
`release/ea_release.json` + `docs/RELEASE_HASHES.json` back; then pin the new EX5 hash per
docs/PRODUCTION_DEPLOY_CHECKLIST.md.

## Verify
- Preflight page: RELEASE_SIGNER row shows **pass — external**.
- `GET https://<signer-host>:9443/healthz` → `{"status":"ok"}`.
- Signature round-trip is verified automatically on first signing call.
