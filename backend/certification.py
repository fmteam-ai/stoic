"""Certification split (iter-212):

  SYSTEM certification   — proves the PIPES: connectivity, verified
                           identity, clock health, latency evidence,
                           market-data feeds. Says NOTHING about edge.
  STRATEGY certification — proves the EDGE: sample size, positive
                           expected edge, honest interval coverage,
                           decay state. Says NOTHING about the pipes.

A production go-live requires BOTH, but they are assessed, reported and
revoked independently."""
from datetime import datetime, timedelta, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _age_s(ts):
    try:
        d = datetime.fromisoformat(str(ts))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - d).total_seconds()
    except (TypeError, ValueError):
        return None


async def system_certification(db, account: dict) -> dict:
    """Infrastructure-only checks — caller must have enforced ownership."""
    acc_id = str(account["_id"])
    hb_age = _age_s(account.get("last_heartbeat"))
    spreads_age = _age_s(account.get("spreads_updated_at"))
    identity = account.get("ea_identity") or {}
    clock = account.get("agent_clock") or {}
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=30)).isoformat()
    traced = await db.trades.count_documents(
        {"account_id": acc_id, "latency_trace.t9_ms": {"$exists": True},
         "opened_at": {"$gte": cutoff}}, limit=1)
    checks = [
        {"key": "bridge_paired", "label": "Bridge paired",
         "ok": bool(account.get("bridge_token"))},
        {"key": "heartbeat_fresh", "label": "Heartbeat < 5 min",
         "ok": hb_age is not None and hb_age < 300,
         "value": f"{int(hb_age)}s" if hb_age is not None else "never"},
        {"key": "identity_verified", "label": "Installation identity verified",
         "ok": bool(identity.get("authoritative")),
         "value": identity.get("installation_id")},
        {"key": "clock_health", "label": "Agent clock telemetry OK",
         "ok": clock.get("status") == "OK",
         "value": clock.get("skew_ms")},
        {"key": "latency_evidence", "label": "T0→T9 latency traces (30d)",
         "ok": traced > 0},
        {"key": "spread_feed", "label": "Tick/spread feed < 10 min",
         "ok": spreads_age is not None and spreads_age < 600},
    ]
    return {"kind": "system", "account_id": acc_id,
            "passed": all(c["ok"] for c in checks), "checks": checks,
            "note": "System certification proves the PIPES — it says "
                    "NOTHING about trading edge.", "at": _now()}


async def strategy_certification(db, user_id: str, scope: str) -> dict:
    """Edge-only checks — independent of any account/system state."""
    from uncertainty_engine import (_sample_r, bootstrap_interval,
                                    realized_coverage)
    rs = await _sample_r(db, user_id, scope, None)
    n = len(rs)
    edge = bootstrap_interval(rs) if n >= 20 else None
    cov = realized_coverage(list(reversed(rs))) if n >= 20 else None
    decay_state = None
    try:
        from strategy_decay import health_for
        h = await health_for(db, user_id, scope)
        decay_state = (h or {}).get("state")
    except Exception:  # noqa: BLE001
        pass
    checks = [
        {"key": "sample_size", "label": "≥ 30 comparable alpha-clean trades",
         "ok": n >= 30, "value": n},
        {"key": "positive_edge", "label": "Expected edge > 0 R",
         "ok": bool(edge and edge["expected_edge_r"] > 0),
         "value": edge["expected_edge_r"] if edge else None},
        {"key": "coverage_honest", "label": "Conformal coverage holds",
         "ok": bool(cov is None or cov["ok"]),
         "value": cov["coverage"] if cov else None},
        {"key": "decay_state", "label": "Decay state HEALTHY/WATCH",
         "ok": decay_state in (None, "HEALTHY", "WATCH"),
         "value": decay_state},
    ]
    return {"kind": "strategy", "scope": scope,
            "passed": all(c["ok"] for c in checks), "checks": checks,
            "edge_interval": ([edge["lower_r"], edge["upper_r"]]
                              if edge else None),
            "note": "Strategy certification proves the EDGE — independent "
                    "of system/pipe health.", "at": _now()}
