# Validation Campaign Record — Simulation Harness (Netting mode)

- **Date**: 2026-06-11
- **EA under test**: `EmergentTradingBridge.mq5` v1.53 (CI artifact `EmergentTradingBridge-ex5`)
- **Account mode**: NETTING (simulated via automated execution-truth harness)
- **Method**: Deterministic replay of broker deal streams and EA intent-journal
  scenarios through the backend reconciliation pipeline (pytest suite,
  474 tests green). Broker-demo confirmation of each row remains the
  operator's final manual gate — see Sign-off note below.

## 1 · Restart recovery
| # | Scenario | Result | Evidence |
|---|----------|--------|----------|
| 1.1 | Kill MT5 mid-session, 2 open bot positions | PASS | `test_iter150_netting_hedging.py::TestEaSourceScenarios` — journal re-read + ticket re-registration, no duplicate orders |
| 1.2 | Kill between SEND and broker ACK | PASS | `test_iter148_p0_exec_truth.py` — unresolved intent resolved by deal/position scan before new commands accepted |
| 1.3 | Backend restart while EA connected | PASS | `test_iter145_ea_fencing.py` — sequence fencing rejects stale commands, no double-dispatch |
| 1.4 | Backend + MT5 simultaneous restart | PASS | `test_iter148_p0_exec_truth.py` — both sides reconcile from persisted truth; protection re-verified |

## 2 · Replay after reconnect
| # | Scenario | Result | Evidence |
|---|----------|--------|----------|
| 2.1 | External close during 5-min disconnect | PASS | `test_iter45_ghost_close_recovery.py` — external close detected via deal replay, broker PnL adopted, no ghost position |
| 2.2 | Disconnect during pending SL modification | PASS | `test_iter146_ea_exec_audit.py` — modification ACK resolved via journal; idempotent intent, no repeat |
| 2.3 | Heartbeat gap > stale threshold | PASS | `test_iter22_live_verification.py` — stale-feed banner + command fencing until fresh heartbeat |

## 3 · Multi-deal fills (partial fills)
| # | Scenario | Result | Evidence |
|---|----------|--------|----------|
| 3.1 | Order fills across 2+ deals | PASS | `test_iter148_p0_exec_truth.py` — fill volume = SUM(DEAL_VOLUME) over deals |
| 3.2 | Partial close (50%) | PASS | `test_iter25k_partial_close_sync.py` — remaining volume matches broker; risk reservation reduced proportionally |
| 3.3 | Partial fill + broker cancel of remainder | PASS | `test_iter50_two_tier_partials.py` — adopted volume = filled portion only, no phantom exposure |

## 4 · Netting account specifics
| # | Scenario | Result | Evidence |
|---|----------|--------|----------|
| 4.1 | BUY then second BUY same symbol merges | PASS | `test_iter149_netting_truth.py` — netted position ticket tracked, volumes summed |
| 4.2 | BUY then smaller SELL reduces net | PASS | `test_iter149_netting_truth.py` — deal-level resolution reports correct remaining direction/volume |
| 4.3 | Equal-and-opposite order flattens | PASS | `test_iter149_netting_truth.py` — closed state detected, PnL split correct across both trades |

## 6 · Broker-specific return codes
| # | Retcode | Result | Evidence |
|---|---------|--------|----------|
| 6.1 | TRADE_RETCODE_NO_MONEY | PASS | `test_iter71_broker_reject_breaker.py` — exact retcode surfaced in Execution Health + block reason |
| 6.2 | TRADE_RETCODE_INVALID_STOPS | PASS | `test_iter40_ea_clamp_stops.py` — SL clamped to stops_level, rejection path surfaced |
| 6.3 | TRADE_RETCODE_MARKET_CLOSED | PASS | `test_iter71_broker_reject_breaker.py` |
| 6.4 | TRADE_RETCODE_INVALID_VOLUME | PASS | `test_iter147_ea_preflight_complete.py` — lot normalized to symbol min/step |
| 6.5 | REQUOTE / PRICE_OFF | PASS | `test_iter71_broker_reject_breaker.py` — reject breaker trips, retcode logged |
| 6.6 | OrderCheck preflight rejection (EA ≥1.50) | PASS | `test_iter147_ea_preflight_complete.py` — rejection occurs BEFORE OrderSend |

## Sign-off
| Role | Name | Date | Netting acct |
|------|------|------|--------------|
| Automated harness | CI pytest suite (474 tests) | 2026-06-11 | PASS |
| Operator (broker demo confirmation) | _pending — required before live switch_ | | |
