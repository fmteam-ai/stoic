"""Iter-152 · Prometheus exposition endpoint (text format, no extra deps).

Protected by METRICS_TOKEN (backend/.env): scrapers send it either as
`Authorization: Bearer <token>` or `X-Metrics-Token: <token>`.
Disabled (503) when the env var is unset — fail fast, no silent default.
"""
import os
import time
from datetime import datetime, timezone

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
    gauge("stoic_unprotected_open", await db.trades.count_documents(
        {"status": "open",
         "$or": [{"stop_loss": {"$in": [None, 0]}},
                 {"lifecycle_state": {"$in": ["FILLED_UNPROTECTED",
                                              "PROTECTION_REQUESTED"]}}]}),
        "Open positions without a broker-confirmed stop")

    gauge("stoic_outbox_pending",
          await db.outbox.count_documents({"state": "pending"}),
          "Outbox commands not yet delivered")
    gauge("stoic_outbox_failed",
          await db.outbox.count_documents({"state": "failed"}))

    now_iso = now.isoformat()
    async for w in db.worker_leases.find({}):
        gauge("stoic_worker_lease_alive",
              1 if str(w.get("expires_at") or "") >= now_iso else 0,
              None, {"worker": str(w.get("_id"))})

    async for a in db.accounts.find(
            {"trading_enabled": {"$ne": False}, "dormant": {"$ne": True},
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

    lines.append("")
    return "\n".join(lines)
