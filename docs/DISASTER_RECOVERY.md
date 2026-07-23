# Disaster Recovery

## What must survive
1. **MongoDB** — the single source of truth (trades, accounts, reservations,
   `trade_events` immutable ledger, outbox, configs).
2. **backend/.env** — secrets (`JWT_SECRET`, `KEY_VAULT_MASTER` especially:
   losing `KEY_VAULT_MASTER` makes stored broker/exchange secrets unrecoverable).
3. **MT5 EA journals** — per-terminal Global Variables (`STOIC.*`); these
   self-heal via replay but shorten reconciliation if preserved.

## Backups
- `deploy/backup.sh backup` — gzip archive + per-collection count manifest in
  `./backups` (14-day retention). Nightly via `deploy/backup.sh schedule`.
- **Encryption**: set `BACKUP_PASSPHRASE_FILE=/path/to/passphrase` — archives
  become AES-256-CBC `.enc` files (PBKDF2, 200k iterations). Keep the
  passphrase OUTSIDE the backup destination.
- **Off-site**: `deploy/backup.sh offsite [file]` pushes archive + manifest via
  rclone (`BACKUP_RCLONE_REMOTE=remote:stoic-backups`) or S3
  (`BACKUP_S3_URI=s3://bucket/stoic`). `BACKUP_OFFSITE=true` pushes
  automatically after every backup.
- **Automated restore verification**: `deploy/backup.sh verify [file]` restores
  the archive into a throwaway Mongo container and compares EVERY collection
  count against the manifest — schedule it nightly (see `schedule`). A backup
  that has never been restore-verified is not a backup.

## Rollback
- During update: `deploy/update.sh` auto-rolls back to the previous ref when
  any post-deploy verification fails.
- Standalone: `deploy/rollback.sh` re-pins the previous entry from
  `deploy/releases.log` (or an explicit ref), rebuilds, and gates on API
  health + full release readiness. `--with-db <archive>` also restores data.
  A safety backup of the current state is always taken first.

## Recovery procedure (total loss)
1. Restore MongoDB from the latest dump.
2. Restore `backend/.env` from the secrets store.
3. Start backend, then frontend (`docker compose up -d` or supervisor).
4. **Do NOT re-enable bots yet.** MT5 terminals will reconnect and the EA will:
   - replay unacknowledged opens from its journal (`ReportOpenFromJournal`),
   - re-resolve `JR_ACCEPTED` orders,
   - heartbeat-reconcile every open position against broker truth.
5. Wait until Bot Health shows: outbox 0 pending, no unresolved submissions,
   all heartbeats fresh, no `UNKNOWN_REQUIRES_RECONCILIATION` trades.
6. Compare open positions in MT5 terminal vs Trades page — they must match 1:1.
7. Re-enable bots one account at a time (demo first).

## Broker failover
There is no automatic broker failover by design (positions are broker-local).
If a broker is down:
1. Bots for that account stop dispatching automatically (heartbeat stale →
   market-data gates fail closed).
2. If positions are open and the broker API is unreachable, use the broker's
   own emergency desk/phone to flatten — record fills manually, then let
   heartbeat reconciliation adopt the external closes.
3. Re-point the account only by creating a NEW account entry (never mutate
   broker credentials of an account with open history).
