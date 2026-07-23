# Incident Response Checklist

## Severity
- **SEV1**: unprotected live positions, unresolved live submissions > 10 min,
  reservation leak, duplicated broker orders.
- **SEV2**: stale feeds on live accounts, outbox backlog growing, worker lease
  flapping, elevated dispatch latency p95.
- **SEV3**: demo-only issues, analytics/UI defects.

## First 5 minutes (any SEV1)
1. Open Dashboard — read the Trading Safety banner (level + which pill is red).
2. If unprotected positions: check the MT5 terminal directly — is the stop on
   the broker position? If not, place it manually in MT5 NOW, then let
   heartbeat reconciliation adopt it.
3. If duplicated/unknown orders: disable bots for that account, then compare
   Trades page vs MT5 positions 1:1.
4. If the platform itself is misbehaving: emergency stop (see ROLLBACK.md) —
   broker-side stops keep protecting positions while the backend is down.

## Diagnosis order
1. `GET /api/bot/safety-status` and `GET /api/bot/execution-health`
   (lifecycle distribution, stuck dispatches, outbox, leases, latency, feeds).
2. Backend logs: `tail -n 200 /var/log/supervisor/backend.err.log`
   (structured access lines carry `rid` for request correlation).
3. `trade_events` ledger for the affected trade_id (immutable audit stream).
4. MT5 Experts tab: `STOIC v1.5x:` prints (preflight rejections, partial
   fills, unresolved accepts, journal replays).

## Exit criteria
- Safety banner green, outbox 0, no unresolved submissions, heartbeats fresh.
- Post-mortem recorded (Loss Lab for trading losses; repo `docs/` for ops).
