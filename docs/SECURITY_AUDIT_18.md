# Security Audit #18 — releases v1.60.10 → v1.60.15 (registry deploys, key rotation, host truth)

Scope: everything added since audit #17 — `deploy/lib.sh` (auto/registry/build provisioning, attestation
fetch/verify/adopt, `publish_release_truth`, `verify_stack_images`), `update.sh` (`--reprovision`,
`--allow-unattested`, `./.env` onboarding defaults), `rollback.sh`, `rotate-release-pin.sh`,
`signer/rotate_fly_signer_key.sh`, `migrate-offline.sh`, `backup.sh restore`, `preflight.sh`/`doctor.sh` key ages,
`host-prereqs.sh`/`docker-root-slave.sh` (M120-1); backend `release_signing.py` (transition/revoked key ids),
`ea_capabilities.py`, `release_truth.py` (host truth under `/app/state`), `key_ages.py`, `key_age_alerts.py`,
`account_cleanup.py` + the DELETE account routes, new readiness checks; `scripts/release_attestation.py`,
`scripts/check_host_python_compat.py`, `verify_ea_release.py`; CI workflows; `ReadinessCard` / `InfraWizard` /
`StatusPage`; `docker-compose.registry.yml`. Method: source review against the threat model "compromised release
assets / GHCR, tampered `deploy/state`, transition-key abuse, secrets on command lines, shell injection from
`.env` / tag names / compose service names / GitHub JSON, TOCTOU on temp files, account-deletion authorization,
unauthenticated disclosure, CI gate code execution". Live VPS not exercised (source-only).

**Verdict: PASS with two P3 hardening items — both fixed in v1.60.15.**

## Summary

| ID | Sev | Location | Finding | Fix |
|---|---|---|---|---|
| SEC-001 | P3 | `backend/release_signing.py` `revoked_key_ids` | Only `RELEASE_REVOKED_KEY_IDS` (host env) revoked a key id. A host that pinned v2 but had not yet run `rotate-release-pin.sh --revoke v1` kept v1 in its transition set, so a v1-signed EA record / policy still verified although the repo marks v1 `revoked`. Needs the exposed v1 private key AND an unfinished rotation; readiness warned loudly. | FIXED: `revoked_key_ids()` = env list ∪ every key id whose committed `release/release_key.fingerprint` status is `revoked` (`_repo_release_key_status`). `transition_key_ids` / `key_id_accepted` / `release_key_rotation_status` follow. Test `test_repo_revoked_status_is_enforced_without_env`. |
| SEC-002 | P3 | `.github/workflows/policy-migration.yml` | `RELEASE_SIGNER_KEY_ID: ${{ secrets.RELEASE_SIGNER_KEY_ID \|\| 'stoic-release-ed25519-v1' }}` — with the secret unset the job would ask the signer for the revoked id (the signer rejects the mismatch and every host rejects v1, so it failed safely; hygiene only). | FIXED: the fallback expression is gone; unset ⇒ `release_signing.key_id()` resolves the repo's `current` fingerprint line (no hard-coded id anywhere since v1.60.13). |

## Re-verified (confirmed safe)

- **Signed-digest supply chain**: `pull_attested_images` cosign-verifies every digest (keyless, identity pinned to
  the repo's Release workflow) before `docker pull @sha256`, and refuses a pulled `RepoDigests[0]` ≠ attested digest;
  the admission manifest pair must verify and name the same digests. A network error (fetch exit 3) can only force an
  **on-host build of the same commit** (never an attacker image); with `ATTESTATION_REQUIRED` (production default) the
  gate still fails closed; a "release not finished" tag is refused (`--allow-unattested` is an explicit operator flag).
- **Overrides are operator-only**: `STOIC_ALLOW_UNATTESTED` and `DEPLOY_MODE_OVERRIDE` are read from the environment
  of a root shell running `update.sh`; they are not reachable from any API/user input and are never persisted.
- **`deploy/state` trust boundary**: `rc_lock.json`, `attestation.current.json`, `deploy_source.json`, `key_ages.json`
  are written by the host after cosign verification and mounted **read-only** into containers; the container cannot
  write them. `deploy_source.json` container data only *tightens* `rc_lock` (adds `containers_stale`), never relaxes
  the signed-digest check; `key_ages.json` is advisory (WARN). Tampering requires host root = total compromise.
  Accepted, documented.
- **Transition keys**: `release_public_key_for` returns a sentinel (`revoked-or-unknown`) for unknown/revoked ids that
  base64-decodes to an invalid key → `verify_hex` fails closed; `key_id_accepted` is checked before any verify call.
- **Secrets on command lines**: `mongosh -p "$(cat /run/secrets/…)"` runs *inside* the mongo container (`sh -c`
  string, not visible on the host's process list beyond the container); GHCR token goes to `docker login
  --password-stdin`; `flyctl secrets set` receives values as argv on the operator's machine only (same as `init_fly_signer.sh`);
  `rotate_fly_signer_key.sh` prints only the public fingerprint, token file paths and the token itself once to the
  operator terminal (documented; files 0600). `migrate-offline.sh` transfers `secrets/` and the passphrase file over
  SSH with `--chmod=600`, refusing a target that runs a stack.
- **Shell injection**: tag names are regex-gated (`^v[0-9]`), compose service names come from `docker compose config`
  JSON via Python, asset filenames are allow-listed in `release_attestation.py`, every expansion is quoted; `eval` of
  image refs is regex-validated to `ghcr.io/…@sha256:[0-9a-f]{64}`.
- **Temp files / atomic writes**: `mktemp` in a private tmp + immediate `rm`; JSON writers use `.tmp` + `os.replace`
  in a root-owned directory (`deploy/state`), no symlink following of attacker-controlled paths.
- **`mongorestore --nsExclude 'admin.*'`**: users/roles come from `mongo-init.js` + this host's secrets; data +
  indexes restore under the restore session; verified against a real auth-enabled mongod in the unit lane.
- **Account deletion**: both DELETE routes resolve the account under the caller's `user_id` *before*
  `cleanup_deleted_account`; the orphan sweep only touches tokens whose account no longer exists.
- **Readiness disclosure**: `/api/ops/release-readiness` (paths, digests, key ids) requires an admin session or the
  metrics token (unchanged); public status page exposes only the signed release identity.
- **CI compat gate**: `check_host_python_compat.py` imports only scripts referenced from the repo's own `deploy/*.sh`
  on a GitHub runner — same trust as running the test-suite.
- Audit #16/#17 controls (attested-environment live gate, JWT alg pin, admin TOTP, TOFU-refusing release-key pin,
  SHA-pinned actions): no regressions.

## Coverage limits
No live VPS session: root-owned `deploy/state` mount semantics, real cosign/registry behaviour and the
`docker compose` container-inspection path were verified with stubs only. No authenticated black-box run.
