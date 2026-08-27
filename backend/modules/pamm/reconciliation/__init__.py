"""Module-level entry to the reconciliation engine + scheduling helper."""
import time

from services.broker_gateway.reconciliation import (  # noqa: F401
    reconcile_all, reconcile_program)

RECON_INTERVAL_SEC = 180
_last_run = 0.0


async def scheduled_reconcile(db) -> list | None:
    """Called from the shared scheduler tick; runs every ~3 minutes."""
    global _last_run
    if time.monotonic() - _last_run < RECON_INTERVAL_SEC:
        return None
    _last_run = time.monotonic()
    return await reconcile_all(db)
