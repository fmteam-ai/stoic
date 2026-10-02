"""Certification split (iter-212, hardened iter-213):

  SYSTEM certification   — proves the PIPES: connectivity, verified
                           identity, clock health, latency evidence,
                           market-data feeds. Says NOTHING about edge.
  STRATEGY certification — proves the EDGE, in TIERS (not a naive
                           positive-edge PASS):
      CERTIFIED_A  ≥100 trades, bootstrap LOWER bound > 0, coverage
                   holds, decay HEALTHY
      CERTIFIED_B  ≥30 trades, expected edge > 0, coverage holds,
                   decay HEALTHY/WATCH
      PROVISIONAL  ≥30 trades, expected edge > 0 (coverage/decay weak)
      UNCERTIFIED  anything else

Issued certifications are PERSISTED with expiry (system 7d, strategy
30d) and can be revoked — validity = issued ∧ not expired ∧ not
revoked ∧ passed at issue time."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

SYSTEM_VALID_DAYS = 7
STRATEGY_VALID_DAYS = 30
TIERS = ("CERTIFIED_A", "CERTIFIED_B", "PROVISIONAL", "UNCERTIFIED")


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return _now_dt().isoformat()


def _age_s(ts):
    try:
        d = datetime.fromisoformat(str(ts))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return (_now_dt() - d).total_seconds()
    except (TypeError, ValueError):
        return None


def strategy_tier(n: int, expected_edge_r, lower_r, coverage_ok: bool,
                  decay_state: str | None) -> str:
    """Pure tier assignment — unit-testable."""
    decay = decay_state or "HEALTHY"
    if (n >= 100 and lower_r is not None and lower_r > 0
            and coverage_ok and decay == "HEALTHY"):
        return "CERTIFIED_A"
    if (n >= 30 and expected_edge_r is not None and expected_edge_r > 0
            and coverage_ok and decay in ("HEALTHY", "WATCH")):
        return "CERTIFIED_B"
    if n >= 30 and expected_edge_r is not None and expected_edge_r > 0:
        return "PROVISIONAL"
    return "UNCERTIFIED"


async def system_certification(db, account: dict) -> dict:
    """Infrastructure-only checks — caller must have enforced ownership."""
    from broker_env import broker_environment
    acc_id = str(account["_id"])
    hb_age = _age_s(account.get("last_heartbeat"))
    spreads_age = _age_s(account.get("spreads_updated_at"))
    identity = account.get("ea_identity") or {}
    clock = account.get("agent_clock") or {}
    cutoff = (_now_dt() - timedelta(days=30)).isoformat()
    traced = await db.trades.count_documents(
        {"account_id": acc_id, "latency_trace.t9_ms": {"$exists": True},
         "opened_at": {"$gte": cutoff}}, limit=1)
    checks = [
        {"key": "bridge_paired", "label": "Bridge paired",
         "ok": bool(account.get("bridge_token") or account.get("bridge_token_hash"))},
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
            "broker_environment": broker_environment(account),
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
    coverage_ok = bool(cov is None or cov["ok"])
    tier = strategy_tier(n, edge["expected_edge_r"] if edge else None,
                         edge["lower_r"] if edge else None,
                         coverage_ok, decay_state)
    checks = [
        {"key": "sample_size", "label": "≥ 30 comparable alpha-clean trades",
         "ok": n >= 30, "value": n},
        {"key": "positive_edge", "label": "Expected edge > 0 R",
         "ok": bool(edge and edge["expected_edge_r"] > 0),
         "value": edge["expected_edge_r"] if edge else None},
        {"key": "lower_bound_clears", "label": "Bootstrap lower bound > 0 R "
         "(tier A requirement)",
         "ok": bool(edge and edge["lower_r"] > 0),
         "value": edge["lower_r"] if edge else None},
        {"key": "coverage_honest", "label": "Conformal coverage holds",
         "ok": coverage_ok, "value": cov["coverage"] if cov else None},
        {"key": "decay_state", "label": "Decay state HEALTHY/WATCH",
         "ok": decay_state in (None, "HEALTHY", "WATCH"),
         "value": decay_state},
    ]
    return {"kind": "strategy", "scope": scope, "tier": tier,
            "passed": tier in ("CERTIFIED_A", "CERTIFIED_B"),
            "checks": checks,
            "edge_interval": ([edge["lower_r"], edge["upper_r"]]
                              if edge else None),
            "note": "Strategy certification proves the EDGE — independent "
                    "of system/pipe health.", "at": _now()}


# ───────────────── issued certifications: expiry + revocation ─────────────

async def issue(db, user_id: str, result: dict) -> dict:
    days = (SYSTEM_VALID_DAYS if result["kind"] == "system"
            else STRATEGY_VALID_DAYS)
    doc = {"cert_id": "cert_" + uuid4().hex[:12],
           "kind": result["kind"],
           "subject": result.get("account_id") or result.get("scope"),
           "tier": result.get("tier"),
           "passed": result["passed"],
           "checks": result["checks"],
           "user_id": user_id,
           "issued_at": _now(),
           "expires_at": (_now_dt() + timedelta(days=days)).isoformat(),
           "revoked": False}
    await db.certifications.insert_one(dict(doc))
    doc.pop("_id", None)
    return doc


def cert_validity(cert: dict, now: datetime | None = None) -> dict:
    """Pure validity rule: issued ∧ passed ∧ not expired ∧ not revoked."""
    now = now or _now_dt()
    expired = str(cert.get("expires_at") or "") <= now.isoformat()
    valid = (bool(cert.get("passed")) and not expired
             and not cert.get("revoked"))
    return {"valid": valid, "expired": expired,
            "revoked": bool(cert.get("revoked"))}


async def active(db, user_id: str, admin: bool = False) -> list:
    q = {} if admin else {"user_id": user_id}
    out = []
    async for c in db.certifications.find(q, {"_id": 0}).sort(
            "issued_at", -1).limit(100):
        out.append({**c, **cert_validity(c)})
    return out


async def revoke(db, cert_id: str, revoked_by: str, reason: str) -> dict:
    r = await db.certifications.find_one_and_update(
        {"cert_id": cert_id, "revoked": False},
        {"$set": {"revoked": True, "revoked_at": _now(),
                  "revoked_by": revoked_by,
                  "revoked_reason": reason}})
    if not r:
        return {"error": "not_found_or_already_revoked"}
    return {"cert_id": cert_id, "revoked": True, "reason": reason}
