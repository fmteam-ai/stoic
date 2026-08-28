"""Intelligence health scopes — the same question ("is the brain
healthy?") answered at three DISTINCT levels so a broken account never
paints the platform red and a platform outage is never hidden inside one
account's noise:

  GLOBAL   — platform subsystems (degraded-intelligence state)
  REGIONAL — per broker/server: connectivity + execution latency
  ACCOUNT  — one trading account: agent link, clock skew, strategy decay
"""
from datetime import datetime, timedelta, timezone

HB_WINDOW_MIN = 5
REGION_P95_YELLOW_MS = 5000


def _hb_cutoff() -> str:
    return (datetime.now(timezone.utc)
            - timedelta(minutes=HB_WINDOW_MIN)).isoformat()


def _connected(acc: dict, cutoff: str) -> bool:
    return acc.get("mode") == "paper" or bool(
        acc.get("last_heartbeat") and str(acc["last_heartbeat"]) >= cutoff)


async def global_health(db, admin: bool = False) -> dict:
    from degraded_intelligence import status
    s = await status(db)
    if not admin:
        for sub in s.get("subsystems", {}).values():
            sub.pop("last_error", None)
    color = ("RED" if s.get("critical_failing")
             else "YELLOW" if s.get("failing") else "GREEN")
    return {"scope": "global", "status": color, **s}


async def regional_health(db, user_id: str | None = None) -> dict:
    """Broker/server-level health: an entire region can degrade (broker
    outage, datacenter latency) without any platform subsystem failing."""
    cutoff = _hb_cutoff()
    q = {"user_id": user_id} if user_id else {}
    regions: dict = {}
    async for a in db.accounts.find(
            q, {"broker": 1, "broker_server": 1, "server": 1,
                "last_heartbeat": 1, "mode": 1}):
        key = str(a.get("broker") or a.get("broker_server")
                  or a.get("server") or "unknown")
        r = regions.setdefault(key, {"accounts": 0, "connected": 0,
                                     "p95s": []})
        r["accounts"] += 1
        r["connected"] += 1 if _connected(a, cutoff) else 0
    from latency_profiler import latency_summary
    ls = await latency_summary(db, days=7, user_id=user_id)
    for g in ls.get("groups", []):
        r = regions.get(g.get("broker"))
        p95 = (g.get("total_ms") or {}).get("p95")
        if r is not None and p95 is not None:
            r["p95s"].append(p95)
    rows = []
    for region, r in regions.items():
        worst_p95 = max(r["p95s"]) if r["p95s"] else None
        if r["accounts"] and not r["connected"]:
            status = "RED"
        elif (r["connected"] < r["accounts"]
              or (worst_p95 or 0) > REGION_P95_YELLOW_MS):
            status = "YELLOW"
        else:
            status = "GREEN"
        rows.append({"region": region, "accounts": r["accounts"],
                     "connected": r["connected"],
                     "worst_p95_total_ms": worst_p95, "status": status})
    order = {"RED": 0, "YELLOW": 1, "GREEN": 2}
    rows.sort(key=lambda x: (order[x["status"]], -x["accounts"]))
    return {"scope": "regional", "regions": rows,
            "status": rows[0]["status"] if rows else "GREEN", "at": _now()}


async def account_health(db, acc: dict) -> dict:
    """Single-account view — caller MUST have already enforced ownership."""
    from broker_env import broker_environment
    acc_id = str(acc["_id"])
    user_id = str(acc.get("user_id") or "")
    connected = _connected(acc, _hb_cutoff())
    from latency_profiler import clock_skew
    sk = await clock_skew(db, user_id=user_id or None)
    my_skew = next((a for a in sk.get("accounts", [])
                    if a["account_id"] == acc_id), None)
    strategies = []
    worst_decay = "HEALTHY"
    order = {"HEALTHY": 0, "WATCH": 1, "DEGRADED": 2, "DECAYING": 3,
             "DISABLED": 4}
    async for h in db.strategy_health.find(
            {"user_id": user_id}, {"scope": 1, "state": 1, "unproven": 1}):
        if h.get("unproven"):
            continue
        state = str(h.get("state") or "HEALTHY")
        strategies.append({"scope": h.get("scope"), "state": state})
        if order.get(state, 0) > order[worst_decay]:
            worst_decay = state
    skew_bad = bool(my_skew and my_skew["status"] != "OK")
    if not connected:
        status = "RED"
    elif skew_bad or worst_decay in ("DECAYING", "DISABLED"):
        status = "YELLOW"
    else:
        status = "GREEN"
    return {"scope": "account", "account_id": acc_id, "status": status,
            "connected": connected,
            "broker": acc.get("broker") or acc.get("broker_server"),
            "broker_environment": broker_environment(acc),
            "clock_skew": my_skew or {"status": "NO_DATA"},
            "clock_telemetry": acc.get("agent_clock")
            or {"status": "NO_DATA",
                "note": "agent has not reported client_time_ms yet"},
            "strategy_decay": {"worst": worst_decay,
                               "strategies": strategies},
            "at": _now()}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
