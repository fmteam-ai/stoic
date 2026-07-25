"""VPS deployment pipeline — state machine, idempotency, broker-first
region recommendation and capacity sizing (infra spec steps 4-8, 21-22)."""
import asyncio
import socket
import statistics
import time
import uuid
from datetime import datetime, timezone

STATES = ["REQUESTED", "PROVISIONING", "SERVER_READY", "BOOTSTRAPPING",
          "AGENT_CONNECTED", "MT5_INSTALLING", "EA_INSTALLING",
          "VALIDATING", "READY", "FAILED"]

# broker endpoint catalogue — measured estimates, never promises
BROKER_CATALOG = [
    {"broker": "RoboForex", "server": "RoboForex-Pro",
     "endpoints": ["robo-pro.roboforex.com:443"],
     "region_estimates_ms": {"london": 4.1, "amsterdam": 7.9,
                             "frankfurt": 11.2, "newyork": 78.6,
                             "tokyo": 212.0}},
    {"broker": "IC Markets", "server": "ICMarketsSC-Live",
     "endpoints": ["h28.p.ctrader.com:443"],
     "region_estimates_ms": {"london": 5.0, "amsterdam": 8.4,
                             "frankfurt": 12.1, "newyork": 74.2,
                             "tokyo": 118.0}},
    {"broker": "OnEquity", "server": "OnEquity-Live",
     "endpoints": ["live.onequity.com:443"],
     "region_estimates_ms": {"london": 3.8, "amsterdam": 7.2,
                             "frankfurt": 11.5, "newyork": 78.4,
                             "tokyo": 205.0}},
    {"broker": "Other / custom", "server": "custom",
     "endpoints": [],
     "region_estimates_ms": {"london": 10.0, "amsterdam": 12.0,
                             "frankfurt": 14.0, "newyork": 60.0,
                             "tokyo": 150.0}},
]

# spec step 6 — capacity from workload
def recommend_capacity(mt5_instances: int, research_workload: bool = False,
                       local_ai: bool = False) -> dict:
    n = max(1, int(mt5_instances))
    if n <= 2 and not local_ai:
        plan = {"plan": "2vcpu-4gb", "vcpu": 2, "ram_gb": 4}
    elif n <= 5:
        plan = {"plan": "4vcpu-8gb", "vcpu": 4, "ram_gb": 8}
    else:
        plan = {"plan": "8vcpu-16gb", "vcpu": 8, "ram_gb": 16}
    if local_ai and plan["vcpu"] < 4:
        plan = {"plan": "4vcpu-8gb", "vcpu": 4, "ram_gb": 8}
    notes = []
    if research_workload:
        notes.append("Research/backtesting belongs on a SEPARATE worker "
                     "VPS — never on the execution VPS.")
    if n > 10:
        notes.append("More than 10 MT5 instances: split across multiple "
                     "execution servers.")
    return {**plan, "mt5_instances": n, "notes": notes}


def _tcp_latency(host: str, port: int, samples: int = 5) -> dict | None:
    vals = []
    for _ in range(samples):
        t0 = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=3):
                vals.append((time.perf_counter() - t0) * 1000)
        except OSError:
            continue
    if not vals:
        return None
    vals.sort()
    return {"median_ms": round(statistics.median(vals), 1),
            "p95_ms": round(vals[int(len(vals) * 0.95) - 1], 1),
            "jitter_ms": round(max(vals) - min(vals), 1),
            "samples": len(vals), "loss_pct": round(
                (samples - len(vals)) / samples * 100, 1)}


async def measure_broker_latency(broker: str) -> dict:
    """Real TCP probe from the backend's own region (best effort) plus the
    catalogued per-region estimates. Always stamped with timestamp+samples."""
    entry = next((b for b in BROKER_CATALOG if b["broker"] == broker),
                 BROKER_CATALOG[-1])
    measured = None
    for ep in entry["endpoints"]:
        host, _, port = ep.partition(":")
        measured = await asyncio.get_event_loop().run_in_executor(
            None, _tcp_latency, host, int(port or 443))
        if measured:
            measured["endpoint"] = ep
            break
    ests = sorted(entry["region_estimates_ms"].items(), key=lambda x: x[1])
    return {"broker": entry["broker"], "server": entry["server"],
            "backend_probe": measured,
            "region_estimates": [{"region": r, "estimate_ms": v}
                                 for r, v in ests],
            "recommended_region": ests[0][0],
            "disclaimer": "measured estimate — not a promise",
            "at": datetime.now(timezone.utc).isoformat(),
            }


async def create_deployment(db, user_id: str, payload: dict,
                            idempotency_key: str | None) -> dict:
    """Idempotent deployment creation — retrying the same key can never
    create a second server (spec step 7)."""
    key = idempotency_key or f"dep_{uuid.uuid4().hex[:10]}"
    existing = await db.vps_deployments.find_one(
        {"user_id": user_id, "idempotency_key": key})
    if existing:
        out = _serialize(existing)
        out["replayed"] = True
        return out
    now = datetime.now(timezone.utc)
    dep = {
        "deployment_id": f"dep_{uuid.uuid4().hex[:10]}",
        "user_id": user_id,
        "idempotency_key": key,
        "path": payload.get("path") or "new_vps",  # new_vps | existing_vps
        "provider": payload.get("provider") or "simulated",
        "region": payload.get("region"),
        "plan": payload.get("plan"),
        "os": "windows-server",
        "broker_profile": payload.get("broker_profile"),
        "mt5_instances": int(payload.get("mt5_instances") or 1),
        # spec step 24 — never provision into a live mode
        "mode": "shadow",
        "state": "REQUESTED",
        "state_history": [{"state": "REQUESTED", "at": now.isoformat()}],
        "server": None,
        "created_at": now, "updated_at": now,
    }
    await db.vps_deployments.insert_one(dep)
    return _serialize(dep)


# simulated timings per state (seconds) — Path A on the simulated provider
_SIM_STEPS = [("REQUESTED", 0), ("PROVISIONING", 2), ("SERVER_READY", 5),
              ("BOOTSTRAPPING", 8), ("AGENT_CONNECTED", 12),
              ("MT5_INSTALLING", 16), ("EA_INSTALLING", 20),
              ("VALIDATING", 24), ("READY", 28)]


async def advance_deployment(db, user_id: str, deployment_id: str) -> dict:
    """Lazy state-machine advance. Simulated provider progresses on a
    timer; real providers progress on provider status + agent events.
    An IP address alone never means READY (spec step 8)."""
    dep = await db.vps_deployments.find_one(
        {"user_id": user_id, "deployment_id": deployment_id})
    if not dep:
        raise ValueError("deployment not found")
    if dep["state"] in ("READY", "FAILED"):
        return _serialize(dep)
    now = datetime.now(timezone.utc)
    created = dep["created_at"]
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    elapsed = (now - created).total_seconds()

    new_state = dep["state"]
    crossed = []
    if dep["provider"] == "simulated":
        for state, at_sec in _SIM_STEPS:
            if elapsed >= at_sec and state != dep["state"]:
                if STATES.index(state) > STATES.index(dep["state"]):
                    crossed.append(state)
        if crossed:
            new_state = crossed[-1]
        if new_state not in ("REQUESTED", "PROVISIONING") \
                and not dep.get("server"):
            from vps_providers import get_provider
            srv = await get_provider("simulated").create_server(
                dep.get("region") or "london",
                dep.get("plan") or "2vcpu-4gb", "windows-server",
                dep["deployment_id"])
            await db.vps_deployments.update_one(
                {"_id": dep["_id"]},
                {"$set": {"server": {"server_id": srv["server_id"],
                                     "ip": "203.0.113.10"}}})
    else:
        # real providers: BOOTSTRAPPING+ requires actual agent events
        agent = await db.vps_agents.find_one(
            {"deployment_id": deployment_id, "revoked": {"$ne": True}})
        if agent:
            new_state = "AGENT_CONNECTED"
            n_inst = await db.mt5_instances.count_documents(
                {"deployment_id": deployment_id})
            if n_inst >= 1:
                new_state = "VALIDATING"
            if n_inst >= dep.get("mt5_instances", 1) \
                    and agent.get("last_heartbeat"):
                new_state = "READY"
    if new_state != dep["state"]:
        history_add = ([{"state": s, "at": now.isoformat()}
                        for s in crossed]
                       or [{"state": new_state, "at": now.isoformat()}])
        await db.vps_deployments.update_one(
            {"_id": dep["_id"]},
            {"$set": {"state": new_state, "updated_at": now},
             "$push": {"state_history": {"$each": history_add}}})
    dep = await db.vps_deployments.find_one({"_id": dep["_id"]})
    return _serialize(dep)


def _serialize(dep: dict) -> dict:
    out = {k: v for k, v in dep.items() if k != "_id"}
    for k in ("created_at", "updated_at"):
        if hasattr(out.get(k), "isoformat"):
            out[k] = out[k].isoformat()
    return out
