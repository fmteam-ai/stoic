"""Scalp subsystem · Step 9 — final fresh-quote execution gate."""
from scalp.state import now_ms


def final_execution_gate(state, cfg, net_edge_pips: float,
                         signal_ts_ms: int, spread_limit_pips: float,
                         health_open_allowed: bool,
                         min_net_edge: float = 0.15,
                         max_signal_age_ms: int = 3000) -> dict:
    checks = {}
    checks["quote_fresh"] = state.quote_age_ms() <= cfg.max_quote_age_ms
    sp = state.spread_pips()
    checks["spread_ok"] = sp is not None and sp <= spread_limit_pips
    checks["signal_fresh"] = (now_ms() - signal_ts_ms) <= max_signal_age_ms
    checks["edge_ok"] = net_edge_pips >= min_net_edge
    checks["health_ok"] = health_open_allowed
    ok = all(checks.values())
    return {"ok": ok, "checks": checks,
            "reason": None if ok else ",".join(k for k, v in checks.items() if not v)}
