# Round 26 — acceptance runbook (what is code, what is operator/CI work)

Status legend: **DONE (code, tested)** · **RUNBOOK** = requires the operator's terminals / CI release / infra.

## P1-01 · release provenance — DONE (code half) + RUNBOOK (tagged build)
Code: `scripts/generate_test_manifest.py --root DIR` and `scripts/freeze_rc_lock.py --root DIR` work on the
STAGED tree; freezing REFUSES a stale test manifest. `release.yml` checks the staged manifest, compares it to the
release commit's, freezes the lock **from `/tmp/pkg`**, runs the strict consistency gate and only then packs the
archive. `deploy/lib.sh verify_release_provenance` gates `install.sh` (before image provisioning) and `update.sh`
(before provisioning, rollback on failure): strict whenever attestation is required; digest mismatches refuse even
a `--skip-attestation` install unless `STOIC_ALLOW_PROVENANCE_DRIFT=1`.
Tests: `tests/unit/test_r26_release_provenance.py`.
Runbook: `git tag -s vX.Y.Z && git push --tags` from a tree whose last commit contains the final test files;
install with `deploy/update.sh vX.Y.Z` (no `--skip-attestation`). AT-P1-01 substitutions are exercised by the
strict gate (`release_consistency_check.py --strict`) — every field must bind to the release commit.

## P1-02 · cryptographic EA installation proof — NOT STARTED (design agreed)
Enrolled installer device key (enrolment code from the dashboard, never the bridge token) → server nonce →
installer signs {nonce, installation_id, terminal identity, EX5 hash, capabilities, ts} → server verifies
signature / freshness / single-use → only that path yields `installer_attested`. AT-P1-02 matrix as tests.

## P1-03 · EA release chain — RUNBOOK
Compile the exact RC MQ5 in the sanctioned Windows/MetaEditor job, record zero errors + tool version,
`scripts/verify_ea_release.py --record` then `--sign`, bind to the release commit. Local compile stays demo-only
(`ea_capabilities.live_gate` requires the pinned release hash).

## P1-04 · production trading truth — RUNBOOK
Signed read-only acceptance pack (`deploy/doctor.sh --bundle`, `GET /api/ops/release-readiness`, AT-01 drill)
against the promoted digests and EA binary. Nothing here is inferred from infrastructure availability.

## P2-01 · unified close coverage — DONE
Every position close is a `close_commands.request_close()` call: manual `POST /trades/{id}/close` (position stays
OPEN; durable `close_seq` + immutable row; the EA closes it through the fenced trades-block path — `poll-trades`
section 1b), scalp `_request_close` (fenced FULL_CLOSE modification stamped atomically), unexpected scalp
reversal, plus every writer migrated in r25. `pending_modification=` stamps `intent_id` + `command_seq` in the
SAME pipeline update. Supersession is exact per `(previous command, same trade)` and self-corrects when a
concurrent writer already allocated a higher sequence (older commands can never stay `requested`).
Guard: `tests/unit/test_r25_close_writers_migrated.py` (no allowlist for writers — only two read-query files).
Protocol tests: `tests/test_r25_close_commands.py`.

## P2-02 · acknowledgement atomicity + fail-closed topology — DONE
`acknowledge_close()` runs ledger + trade pointer in one transaction when no session is supplied. `_with_txn`
raises `TransactionsUnavailable` (HTTP 503 `close_protocol_transactions_unavailable`, nothing written) whenever
`nl_execution.transactions_required(db)` is true (production OR capital-capable); the standalone fallback exists
only under the synthetic-only posture. PANIC (`emergency=True`) is the risk-REDUCING brake: it never refuses,
runs non-transactionally and records `close_protocol_incidents.panic_non_transactional`, surfaced as the
`close_protocol` readiness gate. CI `backend-clean-deploy` now runs the same single-node replica set as production.

## P2-03 · Docker repair safety envelope — DONE (code) + RUNBOOK (disposable-host drill)
Default is DIAGNOSIS ONLY (`diagnose_zombies` prints what would be removed and fails `compose_up`).
`--repair-docker-mounts` / `STOIC_REPAIR_DOCKER_MOUNTS=1` enables mutation: metadata snapshot
(`deploy/releases/docker-repair-snapshots/`), `resolve_zombie` re-resolves every reference to a FULL 64-hex id
that is dead/removing (or a compose-renamed leftover), carries THIS compose project's label and is unambiguous;
metadata removal re-validates immediately before `rm -rf /var/lib/docker/containers/<fullid>`; every mutation is
appended to the hash-chained `deploy/releases/docker-repair-journal.jsonl`, digested into the signed install
report (`docker_repairs`). Tests: `tests/test_iter292_zombie_reaper.py::TestRepairEnvelope` (fake docker shim).
Runbook AT-P2-03: disposable AlmaLinux/cPanel host with unrelated containers — diagnosis-only, explicit repair,
interruption, dockerd restart failure.

## P2-04 · installer exit modes — DONE
`install.sh` readiness policy: `--release-ready` (default for `--production`/`--behind-proxy`: every release gate
clear or exit 2), `--onboarding-close-only` (explicit CLOSE_ONLY acceptance), `--infrastructure-only` (default for
`--dev`). The resulting `deployment_state` (`release_ready` / `onboarding_close_only` / `infrastructure_ready` /
`release_gates_pending`), policy, pending gates and trading posture are persisted in
`deploy/releases/deployment_state.json` and embedded in the signed install report (`deployment`, report v2).
Flags pass through `bootstrap.sh`.

## P2-05 · chart provenance — NOT STARTED (frontend epic)
`SeriesProvenance` {provider, source, as_of, tz, freshness_s, gaps[], cache_state, env, ledger_id} on every chart
payload; reject production payloads missing it; distinct styling for indicative / simulated / reconciled.

## P3-01 · legacy-link diagnosis — DONE
One-time links carry a schema epoch (`v2.` prefix, `activation.TOKEN_SCHEMA`). A failed lookup is classified by
the TOKEN's own format — never by other users' legacy markers: legacy-format → "invalid, already used, expired or
issued before the security upgrade — request a new link"; v2 → "invalid or already used". Telemetry counts by
schema, token material never stored/logged. Tests: `tests/test_r25_token_migration.py`.
