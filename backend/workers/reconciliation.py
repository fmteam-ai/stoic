"""Reconciliation worker — auto-heal, stuck-open sync, scalp sweeps, EOD."""
from workers.base import main

if __name__ == "__main__":
    from background_loops import (_auto_heal_loop, _eod_flatten_loop,
                                  _scalp_reconcile_loop,
                                  _stuck_open_sync_loop)
    from alerting import _ops_alert_loop
    main("reconciliation", [_auto_heal_loop, _stuck_open_sync_loop,
                            _scalp_reconcile_loop, _eod_flatten_loop,
                            _ops_alert_loop])
