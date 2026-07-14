# STOIC — Layer Separation (quant roadmap #1)

Three layers with an enforced import boundary
(`backend/tests/test_iter136_layer_separation.py` fails CI if violated):

## 1. Research layer
- `backend/research/` (experiments, feature studies, ablation notebooks)
- May import anything. NOTHING may import from it.

## 2. Simulation layer
- `backend/backtester/` (event engine, walk-forward runner)
- `backend/ablation.py` (counterfactual gate replay — read-only)
- May import shared libs (risk, strategy_engines, intraday_features).
- MUST NOT import production execution modules (`execution`, `bot_runner`,
  `mt5_bridge`, `crypto_bridge`) — a simulated strategy can never place a
  live order.

## 3. Production layer
- `backend/bot_runner.py`, `backend/execution.py`, `backend/agents/*`,
  `backend/routes/*` (live signal generation, risk approval, execution)
- MUST NOT import from `backtester/` or `research/` — experimental behavior
  can never reach a live account.
- Only versioned strategies (see `versioning.py`) run here; promotion is
  governed by `/app/docs/PROMOTION_CRITERIA.md`.

## Shared pure libraries (importable by all layers)
`risk.py`, `strategy_engines.py`, `payoff_guard.py`, `intraday_features.py`,
`monte_carlo.py`, `pip_utils.py`, `versioning.py` — pure functions, no I/O
side effects on live accounts.
