"""Reconciliation worker — auto-heal, stuck-open sync, scalp sweeps, EOD, execution-intent sweep, PAMM sweep, and
(v1.60.39) every remaining safety loop that server.py only started in single-process mode.

prod v1.60.28 — `_pamm_sweep_loop` was started only by server.py in single-process mode; in external-workers mode
(production) NO worker imported it, so stuck execution intents were never recovered.
prod v1.60.39 — same class of bug for `scheduled_drill_loop` (deployment bake watch / auto-rollback, candidate
auto-promotion, audit-anchor head, scheduled drills), `deploy_watch.loop`, `_heartbeat_watch_loop`,
`_mode_guardian_loop`, `_soak_sampler_loop`, `_soak_tracker_loop`, `_billing_loop`, `ops_outbox_loop` and
`_broker_intel_loop`: the 14 Oct 19:18 promotion's deploy_watch stayed "pending" forever. They all run here now, each
with its own loop_progress heartbeat; `tests/unit/test_v1_60_39_loop_owners.py` asserts every loop started in
server.py's in-process block has an owner under workers/."""
from workers.base import main

if __name__ == "__main__":
    from background_loops import (_auto_heal_loop, _billing_loop, _eod_flatten_loop,
                                  _execution_intents_sweep_loop, _heartbeat_watch_loop,
                                  _mode_guardian_loop, _pamm_sweep_loop, _scalp_reconcile_loop,
                                  _soak_sampler_loop, _soak_tracker_loop, _stuck_open_sync_loop)
    from alerting import _ops_alert_loop
    from broker_intel import _broker_intel_loop
    from gold_bias import gold_bias_loop
    from deploy_watch import loop as _deploy_watch_loop
    from routes.panic_routes import ops_outbox_loop as _ops_outbox_loop
    from scheduled_drills import scheduled_drill_loop
    main("reconciliation", [_auto_heal_loop, _stuck_open_sync_loop,
                            _scalp_reconcile_loop, _eod_flatten_loop,
                            _ops_alert_loop, _execution_intents_sweep_loop,
                            _pamm_sweep_loop,
                            scheduled_drill_loop, _deploy_watch_loop, _heartbeat_watch_loop,
                            _mode_guardian_loop, _soak_sampler_loop, _soak_tracker_loop,
                            _billing_loop, _ops_outbox_loop, _broker_intel_loop, gold_bias_loop])
