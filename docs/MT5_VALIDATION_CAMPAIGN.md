# MT5 Bridge — Final Validation Campaign (pre-live gate)

One dedicated demo-account campaign covering the failure modes that matter
before real capital. **Every item must pass on BOTH a netting and a hedging
demo account** before the live switch. These are validation exercises —
no architectural change is expected.

EA under test: `EmergentTradingBridge.mq5` **v1.53** — use the exact `.ex5`
produced by the `ea-compile` CI job (artifact `EmergentTradingBridge-ex5`),
not a locally compiled binary.

## Evidence status
Automated execution-truth harness evidence is recorded in
[`docs/campaigns/`](campaigns/):
- [`2026-06-11-simulation-harness-netting.md`](campaigns/2026-06-11-simulation-harness-netting.md) — sections 1–4 & 6 PASS (harness)
- [`2026-06-11-simulation-harness-hedging.md`](campaigns/2026-06-11-simulation-harness-hedging.md) — sections 1–3, 5 & 6 PASS (harness)

Every scenario maps to a named test in `backend/tests/` and passes in CI.
The broker-demo confirmation run (real netting + hedging demo accounts) and
the 2-week soak test (section 7) remain the operator's final manual gates
before the live switch.

## How to record results
Copy this file to `docs/campaigns/<date>-<broker>-<netting|hedging>.md`,
fill the PASS/FAIL column and attach the EA log excerpts
(`MQL5/Files/stoic_intent_journal*.txt` + Experts tab logs).

**Machine-readable evidence ledger** (feeds the staged-rollout promotion gate
and the Bot Health "MT5 Validation Campaign" card):
```bash
curl -X POST "$API/api/ops/validation/restart_recovery" \
  -H "X-Metrics-Token: $METRICS_TOKEN" -H "Content-Type: application/json" \
  -d '{"status":"pass","account_mode":"netting",
       "notes":"1.1-1.4 all pass on IC Markets demo 51234567",
       "evidence_ref":"docs/campaigns/2026-06-25-icmarkets-netting.md"}'
```
Scenario keys: `restart_recovery`, `reconnect_recovery`, `duplicate_commands`,
`stale_acknowledgements`, `partial_fills`, `multi_deal_fills`, `netting`,
`hedging`, `rejected_orders`, `emergency_close`,
`manual_broker_intervention`, `long_running_broker_sync`.
Every scenario needs a `pass` record for BOTH `account_mode=netting` and
`account_mode=hedging` before the stage gate allows promotion to `small_live`
(`GET /api/ops/validation` shows progress; `GET /api/ops/stage` the gate).

## 1 · Restart recovery
| # | Scenario | Expected | Result |
|---|----------|----------|--------|
| 1.1 | Kill MT5 mid-session with 2 open bot positions | On restart the EA re-reads the intent journal, re-registers tickets, no duplicate orders | |
| 1.2 | Kill MT5 between order SEND and broker ACK (pull network right after a signal fires) | Journal shows the intent as unresolved; on restart the EA resolves by scanning deals/positions before accepting new commands | |
| 1.3 | Restart backend (docker restart backend worker-*) while EA stays connected | Poll resumes, sequence fencing rejects stale commands, no double-dispatch | |
| 1.4 | Restart backend AND MT5 simultaneously | Both sides reconcile from persisted truth; open positions adopted, protection re-verified | |

## 2 · Replay after reconnect
| # | Scenario | Expected | Result |
|---|----------|----------|--------|
| 2.1 | Disconnect EA network 5 min with an open position, close it manually on the broker's mobile app, reconnect | External close detected via deal replay; trade marked closed with broker PnL, no ghost position | |
| 2.2 | Disconnect during a pending modification (SL move) | Modification ACK resolved via journal on reconnect; no repeated modification (idempotent intent) | |
| 2.3 | Force heartbeat gap > stale threshold | Trading Safety banner shows stale feed; commands fenced until fresh heartbeat | |

## 3 · Multi-deal fills (partial fills)
| # | Scenario | Expected | Result |
|---|----------|----------|--------|
| 3.1 | Order large enough to fill in 2+ deals (thin symbol / large demo lot) | Fill resolved by SUMMING DEAL_VOLUME over deals; reported volume equals broker total | |
| 3.2 | Partial close (50%) of an open position | Remaining volume truth matches broker; risk reservation reduced proportionally | |
| 3.3 | Partial fill followed by broker cancel of the remainder | Adopted volume = filled portion only; no phantom exposure | |

## 4 · Netting account specifics
| # | Scenario | Expected | Result |
|---|----------|----------|--------|
| 4.1 | Open BUY then bot opens second BUY same symbol | Positions merge; backend tracks the netted position ticket, volumes summed | |
| 4.2 | Open BUY then SELL of smaller size | Net position reduced; deal-level resolution reports correct remaining direction/volume | |
| 4.3 | Equal-and-opposite order flattens the position | Position closed state detected; both trades resolved with correct PnL split | |

## 5 · Hedging account specifics
| # | Scenario | Expected | Result |
|---|----------|----------|--------|
| 5.1 | Two same-direction positions same symbol | Two independent position tickets tracked; SL/TP applied per-ticket | |
| 5.2 | Close one of two hedged positions | Correct ticket closed; the other untouched | |

## 6 · Broker-specific return codes
Force each rejection and confirm the backend surfaces the exact retcode in
Execution Health and the account block reason:
| # | Retcode | How to force | Result |
|---|---------|--------------|--------|
| 6.1 | TRADE_RETCODE_NO_MONEY | Demo with tiny balance, oversized lot | |
| 6.2 | TRADE_RETCODE_INVALID_STOPS | SL inside stops_level | |
| 6.3 | TRADE_RETCODE_MARKET_CLOSED | Order outside session | |
| 6.4 | TRADE_RETCODE_INVALID_VOLUME | Lot below symbol min / bad step | |
| 6.5 | TRADE_RETCODE_REQUOTE / PRICE_OFF | High-volatility news spike | |
| 6.6 | OrderCheck preflight rejection (EA ≥1.50) | Any of the above — verify rejection happens BEFORE OrderSend | |

## 7 · Soak test (final gate)
- **Duration**: minimum 2 weeks continuous demo/shadow on the release build.
- **Topology**: full docker-compose stack (API + all 6 workers + Mongo + nginx).
- **Weekly drills** during the soak:
  1. Alert drill — kill worker-trading, confirm alerting fires and lease fails over.
  2. Recovery drill — restore Mongo from the nightly backup into a staging copy, verify equity/trade counts match.
  3. Panic drill — trigger panic, verify flatten + step-up-gated release.
- **Exit criteria**: zero unresolved-accepted states older than 5 min, zero
  duplicate orders, reconciliation delay p95 < 60 s, all alerts delivered,
  audit log complete for every sensitive action.

## Sign-off
| Role | Name | Date | Netting acct | Hedging acct |
|------|------|------|--------------|--------------|
| Operator | | | | |
