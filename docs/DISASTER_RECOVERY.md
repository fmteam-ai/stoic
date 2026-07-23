# Disaster Recovery

## What must survive
1. **MongoDB** — the single source of truth (trades, accounts, reservations,
   `trade_events` immutable ledger, outbox, configs).
2. **backend/.env** — secrets (`JWT_SECRET`, `KEY_VAULT_MASTER` especially:
   losing `KEY_VAULT_MASTER` makes stored broker/exchange secrets unrecoverable).
3. **MT5 EA journals** — per-terminal Global Variables (`STOIC.*`); these
   self-heal via replay but shorten reconciliation if preserved.

## Backups
- Mongo: `mongodump --uri "$MONGO_URL" --db "$DB_NAME"` — run at least hourly
  in production; retain 7 daily + 4 weekly.
- Verify restores monthly: `mongorestore --drop` into a scratch DB, then run
  `cd backend && python -m pytest tests/unit -q` against it.

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
