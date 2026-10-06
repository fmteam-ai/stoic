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

## N100-11 / N101-5 — signer separation (per-purpose tokens, domain prefixes, two keys)
Every signature is Ed25519 over `<domain>\0<data>` with a fixed domain per purpose
(`backend/release_signing.PURPOSES`, mirrored byte-for-byte in `deploy/signer/app.py`):
`ea-release`, `model-manifest`, `policy-migration` (**release purposes — CI / operator**) and
`acceptance-bundle`, `artifact-manifest`, `audit-anchor`, `differentiation`, `canary`
(**runtime purposes — API**). A signature minted for one purpose can never verify as another.
`sign_hex` / `verify_hex` REQUIRE a purpose (N101-5) — there is no default.

The signer enforces WHICH token may request WHICH purpose — **no single-token fallback**:
- `SIGNER_TOKEN` (release token) → release purposes only. Lives in GitHub secret `RELEASE_SIGNER_TOKEN`
  and the signer; it is **never on the API host** (compose hands the API only `signer_token_bundle`;
  `deploy/update.sh` deletes a stray `RELEASE_SIGNER_TOKEN=` line from `backend/.env`; preflight check
  `release_signer_token_scope` FAILS while it is present).
- `SIGNER_TOKEN_BUNDLE` (bundle token) → runtime purposes only. The API reads it as
  `RELEASE_SIGNER_BUNDLE_TOKEN(_FILE)`. Without it the signer runs **release-only** and refuses every
  runtime purpose with 403 (this is the Fly/CI signer's normal state).
  ⇒ a compromised trading API cannot obtain an EA-release or model-manifest signature, and a leaked
  CI token cannot mint acceptance bundles.

Two keys, two ids, two pins (N101-5):
| | key id | pinned in `backend/.env` | signs |
|---|---|---|---|
| CI release key (Fly `stoic-signer`) | `RELEASE_SIGNER_KEY_ID` = `stoic-release-ed25519-v1` | `RELEASE_PUBLIC_KEY_B64` | EA release records, model manifests, policy migrations |
| runtime key (self-hosted sidecar) | `BUNDLE_SIGNER_KEY_ID` = `stoic-bundle-ed25519-v1` (compose `SIGNER_KEY_ID`) | `BUNDLE_PUBLIC_KEY_B64` | acceptance bundles, artifact manifests, anchors, attestations |

`install.sh` pins the sidecar as the runtime key; `update.sh` (`ensure_bundle_key_pins`) migrates an
existing host the same way and leaves `RELEASE_PUBLIC_KEY_B64` as the CI release key — **set it to the
Fly signer's public key** (`flyctl secrets`/`/public-key`) so CI-signed EA records verify on your server.
Unset `BUNDLE_*` = single-key install (both roles use the release key — only valid when the sidecar and
CI really share one private key).

Cut-over notes: pre-N100-11 artefacts signed WITHOUT a prefix are accepted only where history must
stay verifiable (audit anchors, the developer model manifest on re-sign). EA release records and
acceptance bundles are NOT grandfathered — the EA 1.60 record signed before the prefix no longer
verifies (N101-4): **redeploy the Fly signer, then re-run `ea-release`**:
```bash
cd deploy/signer && flyctl deploy -a stoic-signer          # new app.py (purpose-required, release-only)
# GitHub → Actions → ea-release → Run workflow  (commits release/ea_release.json + the EX5)
```
Independent verification of performance attestations: prepend `domain_prefix` from
`POST /api/performance/attestation/verify` (`stoic:differentiation:v1\0`) to `payload_hash`.
