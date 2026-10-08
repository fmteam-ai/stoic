# Changelog

All notable operator-facing changes. Release tags: `vMAJOR.MINOR.PATCH` (signed CI releases).

## v1.60.7 — cPanel/RHEL-8 host prerequisites for existing installs · release-key pin · host suitability
- **deploy/host-prereqs.sh (new)** — idempotent, root-only: `fs.may_detach_mounts=1` (+ `/etc/sysctl.d/99-stoic-docker.conf`), docker root as SLAVE mount (`stoic-docker-root-slave` + `docker.service.d/10-stoic-private-root.conf`, one announced dockerd restart when needed), `host-mount-fix.sh --apply` drop-ins. PASS/FIX/WARN summary; non-zero exit only when (a)/(b) could not be applied. `bootstrap.sh` now calls it (one code path for install + update).
- **deploy/update.sh preflight** (before any build/pull/recreate): applies missing host prerequisites automatically (logged) or refuses with the one-line command under `--no-host-changes`; cleans leftovers of a previous jam (orphan overlay copies, dead/removing/created containers, `<12hex>_stoic-*` rename leftovers — never volumes); one `compose up`, on overlay EBUSY one cleanup + ONE retry, then a clear stop with the reboot recipe instead of a half-recreated stack (no auto-rollback into the same EBUSY).
- **deploy/restart.sh --env-changed / `make apply-env`** — the supported way to apply a `backend/.env` change (same preflight, recreate backend + workers). Plain `docker compose up -d` after an `.env` edit is documented as unsupported.
- **CI release public key** — `install.sh`/`update.sh`/`restart.sh` fetch `GET /public-key` from the public signer (`RELEASE_SIGNER_PUBLIC_URL`, default `https://stoic-signer.fly.dev`), require `key_id == RELEASE_SIGNER_KEY_ID`, show key + fingerprint, confirm (or `--yes`), write the pin; the local sidecar key is never pinned (N102-5). Backend: release purposes (`ea-release`, `model-manifest`) never fall back to the runtime/bundle key; readiness reports *"RELEASE_PUBLIC_KEY_B64 not pinned — … (run deploy/update.sh or see docs/RELEASE_SIGNER.md)"* instead of "signature does NOT verify"; Admin → Release Readiness shows the fix command.
- **Host suitability** — shared web host detection (cPanel/WHM, Plesk, DirectAdmin, httpd/exim/dovecot) in `doctor.sh` and the installer/update preflight (`STOIC_HOST_PROFILE`); production + non-demo-only ⇒ blocking readiness item, demo-only ⇒ warning.
- **Tests / CI** — unit tests for `verify_hex` (no release-purpose fallback, clear unpinned message); shell tests for the preflight branches with stubbed `docker`/`findmnt`/`sysctl`/`systemctl` (prereqs applied + idempotent, `--no-host-changes` refusal, leftovers cleaned, EBUSY retry once then stop, volumes never removed); `shellcheck` on deploy scripts in `static-analysis`.
- No change to trading logic, risk, PANIC or the security agent.

## v1.60.6 — inventory projection ignores the inactive default profile · release summary gate in CI
- Fresh installs no longer fail release-readiness on seed.py's user-default bot profile (`account_id: None, active: False`); only an ACTIVE account-less bot is a structural violation.
- `generate_release_summary.py --check` runs in CI static-analysis so a stale `docs/RELEASE_SUMMARY.md` fails the PR, not the release.

## v1.60.5 — blank-env boot crash fixed (install-from-archive)
- Blank `KEY=` template lines (compose `env_file` + python-dotenv) are treated as unset; Docker `*_FILE` secrets resolve before the sealed-vault overlay. Installer diagnostics dump container logs when the stack does not come up; backend healthcheck gets a start period.
