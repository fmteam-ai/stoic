# Changelog

All notable operator-facing changes. Release tags: `vMAJOR.MINOR.PATCH` (signed CI releases).

## v1.60.8 — main114 review follow-ups (M114-1 … M114-7)
- **M114-1 (P1)** `deploy/docker-root-slave.sh`: a real disk/partition at the docker root is NEVER unmounted (only Docker's own self-bind `<dev>[<root>]` is detached); a real filesystem is bound over itself and demoted in place. Shell test with a stubbed separate partition.
- **M114-2 (P1, live)** `update.sh`/`restart.sh`: when the recreate jams on overlay EBUSY (exit 2) `worker-trading` is stopped first, the jam is journaled and `platform_state.deploy_jam` makes readiness show **TRADING PAUSED** (new `deploy_jam` check + card banner) until a clean `deploy/update.sh` / `deploy/restart.sh --env-changed` clears it (`backend/ops/deploy_jam.py`).
- **M114-3 (P2)** `release/release_key.fingerprint` commits the CI key fingerprint; the preflight refuses to pin a fetched key that differs, warns when an existing pin differs from the committed fingerprint or from what the signer serves (never rewrites it).
- **M114-4 (P2)** `preflight_host` honours `--yes`: without it the dockerd restart is asked on a terminal or refused with the exact command; the harmless sysctl part is still applied.
- **M114-5 (P3)** `scripts/test_unit.sh` uses `generate_test_manifest.py --check` (same as CI) instead of comparing declared functions with collected cases.
- **M114-6 (P3)** `compose_up_guarded`: a second-attempt failure that is not EBUSY returns to the normal rollback path.
- **M114-7 (P3)** host profile is signed by the preflight (HMAC with the ledger anchor key, `STOIC_HOST_PROFILE_SIG`, `STOIC_HOST_DETECTED_AT`) and verified by the backend at read time — a hand-edited `dedicated` shows UNVERIFIED (blocks in production); `host_suitability` is now a PASS/FAIL pill on the Release Readiness card with the detection date.

## v1.60.7 — cPanel/RHEL-8 host prerequisites for existing installs · release-key pin · host suitability
- **deploy/host-prereqs.sh (new)** — idempotent, root-only: `fs.may_detach_mounts=1` (+ `/etc/sysctl.d/99-stoic-docker.conf`), docker root as SLAVE mount (`stoic-docker-root-slave` + `docker.service.d/10-stoic-private-root.conf`, one announced dockerd restart when needed), `host-mount-fix.sh --apply` drop-ins. PASS/FIX/WARN summary; non-zero exit only when (a)/(b) could not be applied. `bootstrap.sh` now calls it (one code path for install + update).
- **deploy/update.sh preflight** (before any build/pull/recreate): applies missing host prerequisites automatically (logged) or refuses with the one-line command under `--no-host-changes`; cleans leftovers of a previous jam (orphan overlay copies, dead/removing/created containers, `<12hex>_stoic-*` rename leftovers — never volumes); one `compose up`, on overlay EBUSY one cleanup + ONE retry, then a clear stop with the reboot recipe instead of a half-recreated stack (no auto-rollback into the same EBUSY).
- **deploy/restart.sh --env-changed / `make apply-env`** — the supported way to apply a `backend/.env` change (same preflight, recreate backend + workers). Plain `docker compose up -d` after an `.env` edit is documented as unsupported.
- **CI release public key** — `install.sh`/`update.sh`/`restart.sh` fetch `GET /public-key` from the public signer (`RELEASE_SIGNER_PUBLIC_URL`, default `https://stoic-signer.fly.dev`), require `key_id == RELEASE_SIGNER_KEY_ID`, show key + fingerprint, confirm (or `--yes`), write the pin; the local sidecar key is never pinned (N102-5). Backend: release purposes (`ea-release`, `model-manifest`) never fall back to the runtime/bundle key; readiness reports *"RELEASE_PUBLIC_KEY_B64 not pinned — … (run deploy/update.sh or see docs/RELEASE_SIGNER.md)"* instead of "signature does NOT verify"; the Release Readiness card on Bot Health shows the fix command.
- **Host suitability** — shared web host detection (cPanel/WHM, Plesk, DirectAdmin, httpd/exim/dovecot) in `doctor.sh` and the installer/update preflight (`STOIC_HOST_PROFILE`); production + non-demo-only ⇒ blocking readiness item, demo-only ⇒ warning.
- **Tests / CI** — unit tests for `verify_hex` (no release-purpose fallback, clear unpinned message); shell tests for the preflight branches with stubbed `docker`/`findmnt`/`sysctl`/`systemctl` (prereqs applied + idempotent, `--no-host-changes` refusal, leftovers cleaned, EBUSY retry once then stop, volumes never removed); `shellcheck` on deploy scripts in `static-analysis`.
- No change to trading logic, risk, PANIC or the security agent.

## v1.60.6 — inventory projection ignores the inactive default profile · release summary gate in CI
- Fresh installs no longer fail release-readiness on seed.py's user-default bot profile (`account_id: None, active: False`); only an ACTIVE account-less bot is a structural violation.
- `generate_release_summary.py --check` runs in CI static-analysis so a stale `docs/RELEASE_SUMMARY.md` fails the PR, not the release.

## v1.60.5 — blank-env boot crash fixed (install-from-archive)
- Blank `KEY=` template lines (compose `env_file` + python-dotenv) are treated as unset; Docker `*_FILE` secrets resolve before the sealed-vault overlay. Installer diagnostics dump container logs when the stack does not come up; backend healthcheck gets a start period.
