# Release key rotation (S-1)

The CI release key (`stoic-release-ed25519-v1`, Fly signer) signs `release/ea_release.json`, model manifests and
policy migrations. Treat it as **exposed** when its private key was ever on an API host or inside a backup —
that was the case on the old cPanel host (the sidecar key doubled as the release key, backups carry `secrets/`).
Rotation = new key id, never a re-key under the same id. The repo decides which key is trusted
(`release/release_key.fingerprint`), the network only delivers it.

## What accepts what

| Where | Variable / file | Meaning |
|---|---|---|
| repo | `release/release_key.fingerprint` | `<key_id> SHA256:<fp> current\|transition\|revoked` — one line per key |
| CI (GitHub secrets) | `RELEASE_SIGNER_KEY_ID`, `RELEASE_PUBLIC_KEY_B64`, `RELEASE_SIGNER_TOKEN` | the key CI signs with |
| API host `backend/.env` | `RELEASE_SIGNER_KEY_ID`, `RELEASE_PUBLIC_KEY_B64` | the **current** pin (fetched by `deploy/preflight.sh`, fingerprint must equal the repo line marked `current`) |
| API host | `RELEASE_ACCEPTED_KEY_IDS`, `RELEASE_TRANSITION_PUBLIC_KEYS` (`<id>=<b64>,…`) | previous key ids whose signatures still verify during the transition |
| API host | `RELEASE_REVOKED_KEY_IDS` | never verify (wins over everything) |

Readiness check `release_key_rotation`: **warn** while a transition key is still accepted, **block** when the set is
inconsistent (current id revoked, transition id without public key). `verify_ea_release.py --check` and
`ea_capabilities` refuse records signed by a key id that is not current/transition.

## Ceremony (operator machine, ~15 min)

1. **Rotate the signer key on Fly** (mints a new key + release token, redeploys, verifies `/public-key`):
   ```bash
   deploy/signer/rotate_fly_signer_key.sh stoic-signer stoic-release-ed25519-v2
   ```
   It prints the fingerprint lines, the GitHub secrets and the server commands below.
2. **Repo** — replace the lines in `release/release_key.fingerprint`:
   ```
   stoic-release-ed25519-v2 SHA256:<new fp> current
   stoic-release-ed25519-v1 SHA256:4c214d393287aac3983178b671e76a35a29d6f0b5e6c815729cc84afaa404d3d transition
   ```
   `.github/workflows/policy-migration.yml` follows the `RELEASE_SIGNER_KEY_ID` secret (falls back to `v1` only when the secret is unset).
3. **GitHub secrets** `RELEASE_SIGNER_KEY_ID=stoic-release-ed25519-v2`, `RELEASE_PUBLIC_KEY_B64=<new>`,
   `RELEASE_SIGNER_TOKEN=<new>` (unless `--keep-token`).
4. **Re-sign** — run the `ea-release` workflow (re-signs `release/ea_release.json`, EA 1.62, under v2) and
   re-sign every signed policy migration you still need (`scripts/sign_policy_migration.py`); the new tag must
   carry a v2-signed record (`release.yml` → `verify_ea_release.py --check`).
5. **Tag** (`v1.60.10`) → Release workflow green.
6. **Every API host**, after `deploy/update.sh v1.60.10` checked out the fingerprint file:
   ```bash
   sudo bash deploy/rotate-release-pin.sh stoic-release-ed25519-v2      # pins v2, keeps v1 as transition, restart --env-changed
   sudo bash deploy/rotate-release-pin.sh --status
   ```
   `update.sh` itself warns (`release key: … is a TRANSITION key — re-pin …`) until this is done.
7. **Finish** — when every host shows the v2 pin and the EA record / policies are v2-signed:
   ```bash
   sudo bash deploy/rotate-release-pin.sh --revoke stoic-release-ed25519-v1
   ```
   and in the repo change the v1 line's status to `revoked`. The readiness warning disappears.
8. **Destroy the exposed private key**: on any host that still has it (`deploy/doctor.sh` → *SECURITY: CI release
   private key present*): `shred -u secrets/signer_ed25519_key.prev-*`; old encrypted backups that contain it stay
   useless for signing once v1 is revoked everywhere.

## Notes
- Production sidecar (runtime/bundle) key is separate (`deploy/rotate-runtime-key.sh`); the pin scripts never touch
  `BUNDLE_*` or `secrets/`.
- `deploy/preflight.sh` refuses to pin a key id marked `revoked`, a key whose fingerprint differs from the repo, or a
  key equal to the local sidecar key (N102-5). First fetch still requires a committed fingerprint (no TOFU).
- A host that cannot reach the public signer: `rotate-release-pin.sh` exits 2 with `RELEASE_PUBLIC_KEY_B64` empty —
  CI-signed records do not verify until the pin is set (paste it from the GitHub secret, fingerprint is checked on
  the next `update.sh`).
