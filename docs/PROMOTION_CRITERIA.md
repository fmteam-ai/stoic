# STOIC — Strategy Promotion Criteria (quant roadmap #18)

Defined BEFORE evaluation. A strategy/engine version may only be promoted to
live production when ALL of the following hold. Criteria may not be changed
after seeing the candidate's results.

## Gate P1 — Sample & validation
- ≥ 100 closed trades in simulation/shadow, spanning ≥ 4 calendar weeks
- Positive net result in at least 5 of 6 walk-forward windows (purged, with embargo)
- Results computed NET of costs: spread, slippage buffer (×1.5), swap

## Gate P2 — Risk quality
- Profit factor ≥ 1.25 after costs
- Max drawdown ≤ 12% of the allocated capital over the test window
- No single symbol contributing > 60% of total profit
- No single week contributing > 40% of total profit
- Stable under parameter perturbation: ±20% on SL/TP multipliers changes
  profit factor by < 0.3

## Gate P3 — Shadow parity (paper_shadow_mode)
- ≥ 2 weeks of shadow trading with the LOCKED candidate version
  (strategy_version stamp must not change mid-test)
- Shadow win rate within ±10pp of simulation
- Realized slippage within 2× the modeled cost assumption
- No safety-gate anomaly: rejection rate per gate within 2× of the
  incumbent version's rate (decision-ledger `stage_counts`)

## Gate P4 — Operational
- All invariant tests pass (`tests/test_iter134_decision_ledger.py`)
- Decision ledger records 100% of the candidate's rejections/executions
- Kill-switch drill: forced RiskAgent exception produces HOLD (fail-closed)

## Demotion (automatic review triggers)
- Scoreboard verdict BLEEDING (PF < 1.0 and negative P&L) over 30 days
- Gate ablation shows the strategy's entries rely on a gate marked
  COSTS EDGE for > 2 consecutive weeks
- Drift detector flags the symbol/strategy for 3 consecutive sessions

## Process
1. Research (notebooks/backtester) → candidate + version stamp
2. Walk-forward validation → P1/P2
3. Shadow mode on one account → P3
4. Human approval → preset applied via `/api/bot/preset/{key}`
5. Scoreboard + ablation monitor the promotion continuously

Versions are immutable: any code change to a live engine bumps
`versioning.STRATEGY_VERSIONS` so attribution stays truthful.
