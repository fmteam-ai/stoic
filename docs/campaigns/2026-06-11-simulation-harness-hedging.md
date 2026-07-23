# Validation Campaign Record — Simulation Harness (Hedging mode)

- **Date**: 2026-06-11
- **EA under test**: `EmergentTradingBridge.mq5` v1.53 (CI artifact `EmergentTradingBridge-ex5`)
- **Account mode**: HEDGING (simulated via automated execution-truth harness)
- **Method**: Deterministic replay of broker deal streams and EA intent-journal
  scenarios through the backend reconciliation pipeline (pytest suite,
  474 tests green). Broker-demo confirmation of each row remains the
  operator's final manual gate — see Sign-off note below.

## 1 · Restart recovery
| # | Scenario | Result | Evidence |
|---|----------|--------|----------|
| 1.1 | Kill MT5 mid-session, 2 open bot positions | PASS | `test_iter150_netting_hedging.py::TestEaSourceScenarios` — per-ticket re-registration on restart, no duplicates |
| 1.2 | Kill between SEND and broker ACK | PASS | `test_iter148_p0_exec_truth.py` — unresolved intent resolved via deal/position scan |
| 1.3 | Backend restart while EA connected | PASS | `test_iter145_ea_fencing.py` — stale-command fencing, no double-dispatch |
| 1.4 | Backend + MT5 simultaneous restart | PASS | `test_iter148_p0_exec_truth.py` — reconciliation from persisted truth |

## 2 · Replay after reconnect
| # | Scenario | Result | Evidence |
|---|----------|--------|----------|
| 2.1 | External close during 5-min disconnect | PASS | `test_iter45_ghost_close_recovery.py` |
| 2.2 | Disconnect during pending SL modification | PASS | `test_iter146_ea_exec_audit.py` — idempotent modification intent |
| 2.3 | Heartbeat gap > stale threshold | PASS | `test_iter22_live_verification.py` — feed fenced until fresh heartbeat |

## 3 · Multi-deal fills (partial fills)
| # | Scenario | Result | Evidence |
|---|----------|--------|----------|
| 3.1 | Order fills across 2+ deals | PASS | `test_iter148_p0_exec_truth.py` — DEAL_VOLUME summation |
| 3.2 | Partial close (50%) | PASS | `test_iter25k_partial_close_sync.py` |
| 3.3 | Partial fill + broker cancel of remainder | PASS | `test_iter50_two_tier_partials.py` |

## 5 · Hedging account specifics
| # | Scenario | Result | Evidence |
|---|----------|--------|----------|
| 5.1 | Two same-direction positions same symbol | PASS | `test_iter150_netting_hedging.py` — two independent tickets tracked, SL/TP per-ticket |
| 5.2 | Close one of two hedged positions | PASS | `test_iter150_netting_hedging.py` — correct ticket closed, sibling untouched |

## 6 · Broker-specific return codes
| # | Retcode | Result | Evidence |
|---|---------|--------|----------|
| 6.1 | TRADE_RETCODE_NO_MONEY | PASS | `test_iter71_broker_reject_breaker.py` |
| 6.2 | TRADE_RETCODE_INVALID_STOPS | PASS | `test_iter40_ea_clamp_stops.py` |
| 6.3 | TRADE_RETCODE_MARKET_CLOSED | PASS | `test_iter71_broker_reject_breaker.py` |
| 6.4 | TRADE_RETCODE_INVALID_VOLUME | PASS | `test_iter147_ea_preflight_complete.py` |
| 6.5 | REQUOTE / PRICE_OFF | PASS | `test_iter71_broker_reject_breaker.py` |
| 6.6 | OrderCheck preflight rejection (EA ≥1.50) | PASS | `test_iter147_ea_preflight_complete.py` — rejection BEFORE OrderSend |

## Sign-off
| Role | Name | Date | Hedging acct |
|------|------|------|--------------|
| Automated harness | CI pytest suite (474 tests) | 2026-06-11 | PASS |
| Operator (broker demo confirmation) | _pending — required before live switch_ | | |
