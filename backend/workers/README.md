# Worker Architecture (Phase F)

Every service runs the SAME codebase; responsibilities are separated into
independently deployable processes. Each worker holds a distributed leader
lease (`worker_leases`, TTL 45s) so replicas are safe — only the leader runs
its loops.

## Service map

| Service              | Entrypoint                       | Responsibilities |
|----------------------|----------------------------------|------------------|
| **API + Broker Gateway + Market Data** | `uvicorn server:app` | HTTP/WebSocket API, `/bridge/*` EA gateway (order dispatch, acks, deals), scalp execution kernel + tick ingest & Phase E data-quality (colocated BY DESIGN: the EA pushes ticks over HTTP and the sub-second scalp loop must share memory with ingest) |
| **Trading worker**   | `python -m workers.trading`      | Main-bot signal loop, market warmer, tiered trade manager |
| **Protection worker**| `python -m workers.protection`   | Unprotected-position repair sweep (10s) — a position without a stop cannot wait for anything else |
| **Reconciliation worker** | `python -m workers.reconciliation` | Auto-heal, stuck-open sync, scalp sweeps (slots / pending deals / invariants), transactional-outbox relay, EOD flatten |
| **Tuning worker**    | `python -m workers.tuning`       | AI optimizer sweeps, nightly quant tuner |
| **Model worker**     | `python -m workers.model`        | Scheduled full retrains per model key (≥200 resolved outcomes), model registry audit trail (`scalp_model_audit`) |
| **Analytics worker** | `python -m workers.analytics`    | DB-only daily aggregates (`scalp_daily_stats`): decision counts, win rate, net pips, decision-quality averages, reject-stage mix |

## Modes

- **In-process (default, preview)**: `BACKGROUND_WORKERS_IN_PROCESS=true` —
  the API process runs every loop (single-pod simplicity).
- **Separated (production)**: set `BACKGROUND_WORKERS_IN_PROCESS=false` on
  the API and run each worker as its own deployment/container. API restarts
  then never interrupt trading, protection or reconciliation.

## Notes

- Workers are DB-coordinated only (no RPC between them); anything needing
  the in-memory scalp runners lives with the API process.
- The scalp model retrains inline in the API (event-driven, cheap); the
  model worker guarantees a persisted, audited retrain cadence on top.
