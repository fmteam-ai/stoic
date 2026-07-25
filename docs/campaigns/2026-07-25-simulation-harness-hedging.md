# Validation Campaign Evidence — HEDGING (automated harness)

- **Run ID**: `harness:82a08884d7c7`
- **Executed at**: 2026-07-25 22:54:10.831000 UTC
- **EA under test**: EmergentTradingBridge v1.55 protocol (simulated EA speaking the real bridge endpoints against the running backend)
- **Result**: **12/12 scenarios PASS**, 0 fail
- **Ledger**: every scenario recorded in `validation_evidence` (evidence_ref `harness:82a08884d7c7`)

| Scenario | Status | Notes |
|----------|--------|-------|
| `restart_recovery` | PASS | order re-dispatched after simulated EA crash; dispatch_count=2; no duplicate trade docs created |
| `reconnect_recovery` | PASS | reconnect heartbeat accepted, equity updated to 10123.45, last_heartbeat refreshed |
| `duplicate_commands` | PASS | immediate second poll did NOT re-dispatch the order (30s atomic dispatch lock held) |
| `stale_acknowledgements` | PASS | stale ack absorbed (HTTP 200), unknown-trade ack handled (HTTP 404), trade state intact |
| `partial_fills` | PASS | partial fill recorded: lot_size=0.04 (requested 0.10), partial_fill=True, status open |
| `multi_deal_fills` | PASS | 2 in-deals stored once each (2/2), duplicate re-push idempotent, exactly 1 open trade for the position |
| `netting` | PASS | full-volume opposite deal netted the position: status=closed, pnl=50.0 |
| `hedging` | PASS | hedged BUY+SELL coexisted then closed independently (pnl 30.0/-10.0) |
| `rejected_orders` | PASS | rejected order marked failed (status=failed), no phantom open position |
| `emergency_close` | PASS | FULL_CLOSE dispatched to EA and broker close deal reconciled (status=closed, pnl=-20.0) |
| `manual_broker_intervention` | PASS | manual broker trade ingested (origin=manual) and closed with broker P&L 40.0 |
| `long_running_broker_sync` | PASS | sync dispatched once (180s re-dispatch guard held), completion cleared pending flag and stamped last_full_sync |

Manual operator gates that remain before the live switch:
real-broker demo confirmation on an actual terminal and the 2-week
soak (docs/campaigns/SOAK_LOG.md).
