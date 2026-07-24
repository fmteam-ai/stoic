# Silent Exception Suppression Audit — financial paths (2026-07-24)

Scope: `except Exception: pass` / silent suppression in code that touches
order execution, risk state, or governance. Requested in safety review.

## Fixed this pass
- `bot_runner.py` mode gate: failed OPEN to autonomous_live → now fails
  CLOSED to observe with `logger.error` (a broken gate can never trade).
- `bot_runner.py` record_intercept: bare `pass` → `logger.warning`
  (skip is still enforced either way).
- `loss_advisor.py` governance-ledger records (2 sites): bare `pass` →
  `logger.warning` (measure/guard still applied).

## Reviewed, acceptable as-is (fail-open is the SAFE direction)
- `bot_runner.py` trend-score stamp: informational only; `logger.debug`.
- `bot_runner.py` sweep blocks (friday_flat, loss_advisor, self-eval,
  learning records, drift…): each wrapped with `logger.exception` — loud,
  not silent. A sweep crash must not kill the scan loop.
- `learning_record.py` field gathering (`commission/swap`, events): partial
  record beats no record; classification wrapper logs at debug.
- `failure_classifier.py` evidence gathering: fail-open to
  `normal_statistical_loss` — classification is advisory, never execution.
- `ops_routes.py` readiness auth probes: try both auth paths by design.

## Watchlist (pre-existing, outside this pass's scope)
- `execution.py` / `bridge_routes.py`: reviewed in iter-134 event-sourcing
  work; failures there raise or write `trade_events` — not silent.
- Any NEW code touching order placement must follow: fail CLOSED, log at
  warning or above, and record a trade_event where a trade is involved.
