# Round 25 — acceptance runbook (what is code, what is operator/CI work)

Status legend: **DONE (code, tested)** · **RUNBOOK** = requires the operator's terminals / CI release / infra.

## P1-01 · EA binary proof — DONE (code) + RUNBOOK (roll-out)
Chain: `STOIC-Installer.ps1` measures the deployed `EmergentTradingBridge.ex5` (SHA-256) →
writes `MQL5\Files\STOIC-Proof.txt` → `POST /api/infra/agent/artifact-digest` binds the hash to the
`installation_id` (installer-only, same account) → EA reads the file and sends `ea_binary_sha256` on every
heartbeat → `bridge_routes` grants `installer_attested` only when heartbeat hash == installer-measured hash for
that installation → `ea_capabilities.live_gate` admits live only when hash == signed release hash **and**
method == `installer_attested`. Everything else → `EA_BINARY_PROOF_UNATTESTED` / `_MISMATCH` / `_MISSING`.
Tests: `tests/unit/test_r25_ea_binary_proof.py` (matrix AT-P1-01).

Roll-out (per terminal): pin the release hash (`scripts/verify_ea_release.py --record` after a MetaEditor
compile of the RC source) → run the installer one-liner from Connect MT5 on the terminal → attach the EA →
Admin → Ops shows `ea_binary_sha256_method=installer_attested`. Re-run the installer after every EA upgrade.
Known limit: the installer measures at install time; a later on-disk modification is caught only by the release
hash mismatch of what the EA echoes if the EA is rebuilt, or by re-running the installer / host agent.

## P1-02 · authoritative release — RUNBOOK
`git tag -s vX.Y.Z && git push --tags` → CI `release.yml` builds backend+frontend images, writes strict
`release/rc_lock.json` (authoritative=true), SBOMs, signs the pair, publishes the EX5 hash → install/upgrade
**without** `--skip-attestation`: `sudo bash /opt/stoic/deploy/update.sh vX.Y.Z`. Gates `release_attestation`
and `rc_lock` clear automatically.

## P1-03 · signed operational acceptance bundle — RUNBOOK
After 6 accounts + approved 6/3/3 inventory: `sudo bash /opt/stoic/deploy/doctor.sh --bundle` (signed by the
sidecar) + `GET /api/ops/release-readiness` (admin) must show `ready:true`; keep the JSON with the release tag.

## P1-04 · external signer — RUNBOOK
Provision the isolated signer (docs/SIGNER.md), set `RELEASE_SIGNER_URL/_TOKEN/_CA` and
`RELEASE_SIGNER_DEFERRED=false`, verify `GET /api/release-key` key_id + canary, then re-run readiness.

## P2-01 · close protocol — DONE (core + writers migrated)
`close_commands.py`: atomic per-trade `close_seq` (findOneAndUpdate), unique index `(trade_id, close_seq)`,
transaction wrapper when no session is supplied (replica set), idempotent seq-bound `acknowledge_close`
that also updates the trade pointer. Tests: `tests/test_r25_close_commands.py` (6 concurrent writers).
All position-close writers now call `request_close()` (protection guard, pre-news protector, Friday flat,
EOD flatten, TP2/TP3 full close, portfolio deleverage, Telegram /panic + /close, diagnostic excess-over-cap,
NL, PANIC). Guard: `tests/unit/test_r25_close_writers_migrated.py` refuses new direct `close_requested` writers.
Remaining literal sites are queries, the pending-order verb (`trade_routes`) or the scalp fenced path.

## P2-02 · PANIC outbox — DONE
Transient websocket failure → `pending` + exponential `not_before` backoff; `failed` only after
`OUTBOX_MAX_ATTEMPTS`; sweeper honours `not_before`; `/api/ops/release-readiness` exposes `panic_outbox`
counts. Tests: `tests/unit/test_r25_panic_outbox.py`.

## P2-03 · token migration — DONE
Startup one-shot `invalidate_legacy_plaintext_tokens()` (idempotent); consumers answer `token_superseded`
with a resend path; `auth_token_failures` counts by schema (token never logged). Tests:
`tests/unit/test_r25_token_migration.py`.

## P2-04 · chart provenance — RUNBOOK (frontend epic)
Add `SeriesProvenance` {provider, source, as_of, tz, freshness_s, gaps[], cache_state, env, ledger_id} to
every chart payload; reject production payloads missing it; distinct styling for indicative/simulated/reconciled.

## P2-05 · signer host boundary — RUNBOOK
Production: remote signer/KMS per docs/SIGNER.md; allowlist host; pin CA/public key; rotate credentials.

## Operator drills (AT-P2-04/05)
Turnstile matrix on staging with `TURNSTILE_SITE_KEY/SECRET`; fresh-host install / interrupted install /
`update.sh` / `rollback.sh` / `backup.sh restore` / key rotation on a disposable AlmaLinux box using the same
one-liner (`--behind-proxy` and `--cloudflare` modes).
