"""Dedicated worker entry points (review item 11).

    python -m workers.trading         # bot runner + trade manager + warmer
    python -m workers.reconciliation  # auto-heal, stuck-sync, scalp sweeps, EOD
    python -m workers.tuning          # optimizer + nightly tuner

Each worker holds a Mongo leader lease so accidental duplicate replicas
stand by instead of double-running. Set BACKGROUND_WORKERS_IN_PROCESS=false
on the API service when these run externally.

Example supervisor entries (production):
    [program:worker_trading]
    command=python -m workers.trading
    directory=/app/backend
    autorestart=true
"""
