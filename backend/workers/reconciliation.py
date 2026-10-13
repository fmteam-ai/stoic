"""Reconciliation worker — auto-heal, stuck-open sync, scalp sweeps, EOD, execution-intent sweep, PAMM sweep.

prod v1.60.28 — `_pamm_sweep_loop` (PAMM risk sweeps + broker heartbeats + the intent sweeps) was started only by
server.py in single-process mode; in external-workers mode (production) NO worker imported it, so stuck execution
intents were never recovered. Both sweeps now run here, each with its own loop_progress heartbeat."""
from workers.base import main

if __name__ == "__main__":
    from background_loops import (_auto_heal_loop, _eod_flatten_loop,
                                  _execution_intents_sweep_loop,
                                  _pamm_sweep_loop, _scalp_reconcile_loop,
                                  _stuck_open_sync_loop)
    from alerting import _ops_alert_loop
    main("reconciliation", [_auto_heal_loop, _stuck_open_sync_loop,
                            _scalp_reconcile_loop, _eod_flatten_loop,
                            _ops_alert_loop, _execution_intents_sweep_loop,
                            _pamm_sweep_loop])
