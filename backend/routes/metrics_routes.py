"""Iter-152 · Prometheus exposition endpoint (text format, no extra deps).

Protected by METRICS_TOKEN (backend/.env): scrapers send it either as
`Authorization: Bearer <token>` or `X-Metrics-Token: <token>`.
Disabled (503) when the env var is unset — fail fast, no silent default.
"""
import os
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import PlainTextResponse

from database import get_db

router = APIRouter(tags=["metrics"])


def _authorized(request: Request) -> bool:
    import hmac
    expected = os.environ.get("METRICS_TOKEN")
    if not expected:
        raise HTTPException(status_code=503, detail="metrics disabled")
    got = request.headers.get("X-Metrics-Token")
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        got = got or auth[7:]
    return bool(got) and hmac.compare_digest(str(got), expected)


def _esc(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"')


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(request: Request):
    if not _authorized(request):
        raise HTTPException(status_code=403, detail="bad metrics token")
    db = get_db()
    now = datetime.now(timezone.utc)
    lines = []

    def gauge(name, value, help_txt=None, labels=None):
        if help_txt:
            lines.append(f"# HELP {name} {help_txt}")
            lines.append(f"# TYPE {name} gauge")
        lbl = ""
        if labels:
            lbl = "{" + ",".join(f'{k}="{_esc(v)}"'
                                 for k, v in labels.items()) + "}"
        lines.append(f"{name}{lbl} {value}")

    t0 = time.perf_counter()
    await db.command("ping")
    gauge("stoic_mongo_latency_ms",
          round((time.perf_counter() - t0) * 1000, 2), "MongoDB ping RTT")

    for st in ("open", "pending"):
        gauge("stoic_trades", await db.trades.count_documents({"status": st}),
              "Trades by status" if st == "open" else None, {"status": st})

    gauge("stoic_unresolved_submissions", await db.trades.count_documents(
        {"status": "pending",
         "submission_state": "broker_accepted_unresolved"}),
        "Broker-accepted orders awaiting position resolution")
    from protection_guard import unprotected_open_query
    gauge("stoic_unprotected_open",
          await db.trades.count_documents(unprotected_open_query()),
        "Open positions without a broker-confirmed stop")

    gauge("stoic_outbox_pending",
          await db.outbox.count_documents({"state": "pending"}),
          "Outbox commands not yet delivered")
    gauge("stoic_outbox_failed",
          await db.outbox.count_documents({"state": "failed"}))

    now_iso = now.isoformat()
    leases = {str(w.get("_id")): w async for w in db.worker_leases.find({})}
    # every EXPECTED worker is always reported — an absent lease is 0 (dead),
    # never a missing series that dashboards silently ignore
    from routes.ops_routes import EXPECTED_WORKERS
    for wname in sorted(set(EXPECTED_WORKERS) | set(leases)):
        w = leases.get(wname) or {}
        gauge("stoic_worker_lease_alive",
              1 if str(w.get("expires_at") or "") >= now_iso else 0,
              None, {"worker": wname})
        if w.get("loops_total") is not None:
            gauge("stoic_worker_loops_total", w["loops_total"],
                  None, {"worker": wname})
            gauge("stoic_worker_loops_running", w.get("loops_running") or 0,
                  None, {"worker": wname})
        for ln, st in (w.get("loops") or {}).items():
            lbl = {"worker": wname, "loop": ln}
            gauge("stoic_worker_loop_restart_count",
                  (st or {}).get("restart_count", 0), None, lbl)
            gauge("stoic_worker_loop_consecutive_failures",
                  (st or {}).get("consecutive_failures", 0), None, lbl)
            gauge("stoic_worker_loop_processed_total",
                  (st or {}).get("processed_count", 0), None, lbl)
            if (st or {}).get("last_duration_ms") is not None:
                gauge("stoic_worker_loop_last_duration_ms",
                      st["last_duration_ms"], None, lbl)
            _done = (st or {}).get("last_iteration_completed_at")
            try:
                _done = (_done if isinstance(_done, datetime)
                         else datetime.fromisoformat(str(_done)))
                if _done.tzinfo is None:
                    _done = _done.replace(tzinfo=timezone.utc)
                gauge("stoic_worker_loop_last_iteration_age_seconds",
                      int((now - _done).total_seconds()), None, lbl)
            except Exception:
                pass
        if wname == "reconciliation":
            try:
                renewed = datetime.fromisoformat(str(w.get("renewed_at")))
                if renewed.tzinfo is None:
                    renewed = renewed.replace(tzinfo=timezone.utc)
                gauge("stoic_reconciliation_lease_age_seconds",
                      int((now - renewed).total_seconds()),
                      "Age of the reconciliation worker lease renewal")
            except Exception:
                pass

    # queue age — oldest pending outbox event
    oldest = await db.outbox.find_one({"state": "pending"},
                                      sort=[("created_at", 1)],
                                      projection={"created_at": 1})
    if oldest and oldest.get("created_at"):
        try:
            ts = oldest["created_at"]
            if not isinstance(ts, datetime):
                ts = datetime.fromisoformat(str(ts))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            gauge("stoic_outbox_oldest_pending_age_seconds",
                  int((now - ts).total_seconds()),
                  "Age of the oldest undelivered outbox event")
        except Exception:
            pass

    # oldest broker command still pending (submit queue age)
    pend = await db.trades.find_one(
        {"status": "pending"}, sort=[("created_at", 1)],
        projection={"created_at": 1, "updated_at": 1})
    if pend:
        try:
            ts = datetime.fromisoformat(
                str(pend.get("created_at") or pend.get("updated_at")))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            gauge("stoic_pending_trade_oldest_age_seconds",
                  int((now - ts).total_seconds()),
                  "Age of the oldest broker command awaiting execution")
        except Exception:
            pass

    # execution SLO — fill → broker-confirmed protection latency (24h window)
    try:
        cutoff = (now - timedelta(hours=24)).isoformat()
        samples = []
        async for ev in db.trade_events.find(
                {"event_type": "ProtectionPlaced",
                 "occurred_at": {"$gte": cutoff},
                 "trade_id": {"$ne": None}},
                projection={"trade_id": 1, "occurred_at": 1}).sort(
                    "occurred_at", -1).limit(500):
            from bson import ObjectId as _OID
            try:
                tr = await db.trades.find_one({"_id": _OID(ev["trade_id"])},
                                              {"opened_at": 1})
            except Exception:
                tr = None
            if not tr or not tr.get("opened_at"):
                continue
            try:
                opened = datetime.fromisoformat(str(tr["opened_at"]))
                placed = datetime.fromisoformat(str(ev["occurred_at"]))
                delta = (placed - opened).total_seconds()
                if 0 <= delta < 3600:
                    samples.append(delta)
            except Exception:
                continue
        gauge("stoic_protection_latency_samples", len(samples),
              "Fill→protection latency samples in the last 24h")
        if samples:
            samples.sort()
            def pct(p):
                return round(samples[min(len(samples) - 1,
                                         int(p * len(samples)))], 2)
            for q, v in (("p50", pct(0.50)), ("p95", pct(0.95)),
                         ("max", samples[-1])):
                gauge("stoic_protection_latency_seconds", v,
                      None, {"quantile": q})
    except Exception:
        pass

    # alert acknowledgement tracking
    for sev in ("critical", "warning", "info"):
        gauge("stoic_alerts_unacked", await db.ops_alerts.count_documents(
            {"acked_at": None, "severity": sev}),
            "Unacknowledged ops alerts" if sev == "critical" else None,
            {"severity": sev})
    oldest_alert = await db.ops_alerts.find_one(
        {"acked_at": None}, sort=[("created_at", 1)],
        projection={"created_at": 1})
    if oldest_alert:
        try:
            ts = datetime.fromisoformat(str(oldest_alert["created_at"]))
            gauge("stoic_alert_oldest_unacked_age_seconds",
                  int((now - ts).total_seconds()),
                  "Age of the oldest unacknowledged alert")
        except Exception:
            pass

    async for a in db.accounts.find(
            {"trading_enabled": True, "dormant": {"$ne": True},  # audit P1-6
             "status": {"$ne": "deleted"}},
            {"label": 1, "last_heartbeat": 1, "broker_utc_offset_sec": 1}):
        label = a.get("label") or str(a["_id"])[-6:]
        try:
            hb = datetime.fromisoformat(str(a.get("last_heartbeat")))
            if hb.tzinfo is None:
                hb = hb.replace(tzinfo=timezone.utc)
            gauge("stoic_ea_heartbeat_age_seconds",
                  int((now - hb).total_seconds()), None, {"account": label})
        except Exception:
            pass
        if a.get("broker_utc_offset_sec") is not None:
            gauge("stoic_broker_clock_offset_seconds",
                  int(a["broker_utc_offset_sec"]), None, {"account": label})

    from ws_manager import manager as ws_manager
    gauge("stoic_ws_clients",
          sum(len(s) for s in ws_manager._connections.values()),
          "Connected websocket clients")

    # Event-loop lag (runtime_watchdog probe, per process).
    try:
        from runtime_watchdog import prometheus_lines
        lines.extend(prometheus_lines())
    except Exception:  # noqa: BLE001
        pass

    lines.append("")
    return "\n".join(lines)
