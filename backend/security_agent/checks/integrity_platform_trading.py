"""Data integrity (I1–I4), Platform (P1–P5), Trading health (T1–T2)."""
import os
import shutil
from datetime import datetime, timedelta, timezone

from security_agent.checks.common import f, iso_ago, now, th


# ── integrity ───────────────────────────────────────────────────────────────
async def I1(db, cfg):
    from seed import duplicate_tickets
    groups = await duplicate_tickets(db, limit=50)
    return [f("I1", f"account:{g['account_id']}:ticket:{g['mt5_ticket']}", "high", "integrity",
              f"{g.get('count', 2)} live rows share ticket {g['mt5_ticket']} on account {g['account_id']}", g) for g in groups]


# A13-2 / main98 — bridge tokens are stored hashed: the unique index is on bridge_token_hash (bridge_tokens.ensure_indexes
# drops the legacy plaintext bridge_token_1), so I2 must require the hash index or it alerts forever.
REQUIRED_INDEXES = {"users": "email_1", "accounts": "bridge_token_hash_1", "trades": "uniq_account_ticket",
                    "broker_deals": None, "order_authorizations": "nonce_1", "audit_anchors": "seq_1"}


async def I2(db, cfg):
    out = []
    for coll, name in REQUIRED_INDEXES.items():
        info = await db[coll].index_information()
        if name is None:
            ok = any(i.get("unique") and [k for k, _ in i["key"]] == ["deal_id", "account_id"] for i in info.values())
            name = "uniq(deal_id, account_id)"
        else:
            ok = name in info and info[name].get("unique", False)
        if not ok:
            out.append(f("I2", f"{coll}:{name}", "critical", "integrity", f"required unique index {name} missing on {coll}", {"collection": coll, "index": name}))
    return out


async def I3(db, cfg):
    from audit_chain import verify_chain
    res = await verify_chain(db)
    if res.get("ok", True):
        return []
    return [f("I3", "admin_audit_log", "critical", "integrity", f"admin audit chain broken at seq {res.get('broken_at') or res.get('first_bad_seq')}",
              {k: v for k, v in res.items() if k != "ok"})]


async def I4(db, cfg):
    bdir = os.environ.get("STOIC_BACKUP_DIR") or os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "backups")
    newest = None
    try:
        for n in os.listdir(bdir):
            if n.endswith(".archive.gz"):
                m = datetime.fromtimestamp(os.path.getmtime(os.path.join(bdir, n)), tz=timezone.utc)
                newest = m if newest is None or m > newest else newest
    except OSError:
        # S13 — no backup folder mounted: report "no data" (mount it read-only on the worker) instead of silence
        return [f("I4", "no_data", "low", "integrity", f"backup folder {bdir} is not readable from the security worker — "
                  "mount it read-only (STOIC_BACKUP_DIR) so backup age can be checked", {"dir": bdir, "no_data": True})]
    max_h = th(cfg, "I4", "max_backup_age_h", 26)
    if newest is None or now() - newest > timedelta(hours=max_h):
        age = "never" if newest is None else f"{(now() - newest).total_seconds() / 3600:.1f} h ago"
        return [f("I4", "backup", "high", "integrity", f"last good Mongo backup: {age} (limit {max_h} h)", {"dir": bdir, "newest": newest.isoformat() if newest else None})]
    rt = await db.platform_state.find_one({"_id": "backup_restore_test"})
    if rt and rt.get("ok") is False:
        return [f("I4", "restore_test", "high", "integrity", f"backup restore test failed at {rt.get('at')}", {k: v for k, v in rt.items() if k != "_id"})]
    return []


# ── platform ────────────────────────────────────────────────────────────────
async def P1(db, cfg):
    out = []
    async for a in db.ops_alerts.find({"acked_at": None, "kind": {"$in": ["worker_lease_expired", "worker_loop_crashloop", "worker_loop_stalled"]}}).limit(50):
        out.append(f("P1", f"{a['kind']}:{a.get('dedup_key')}", "high", "platform", a.get("message") or a["kind"], {"kind": a["kind"], "since": a.get("created_at") or a.get("at")}))
    cutoff = now()
    async for w in db.worker_leases.find({}):
        exp = w.get("expires_at")
        if isinstance(exp, str):
            try:
                exp = datetime.fromisoformat(exp.replace("Z", "+00:00"))
            except ValueError:
                exp = None
        if exp is not None and exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp is not None and exp < cutoff:
            out.append(f("P1", f"lease:{w['_id']}", "high", "platform", f"worker {w['_id']} lease expired at {exp.isoformat()}", {"worker": str(w["_id"])}))
    return out


async def P2(db, cfg):
    try:
        from slo import compute_slos
        s = await compute_slos(db)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for name, v in (s.get("slos") or s).items():
        if isinstance(v, dict) and v.get("ok") is False:
            out.append(f("P2", f"slo:{name}", "medium", "platform", f"SLO {name} breached: {v.get('actual_pct', v.get('value'))} vs target {v.get('target_pct')}", v))
    return out


async def P3(db, cfg):
    out = []
    since = iso_ago(minutes=10)
    async for c in db.runtime_crash_log.find({"at": {"$gte": since}}).limit(20):
        kind = "blocked" if c.get("blocked_for_s") else "crash"
        out.append(f("P3", f"{kind}:{c.get('process') or c.get('role') or 'api'}", "high", "platform",
                     f"event loop {kind} in {c.get('process') or c.get('role') or 'api'}" + (f" for {c['blocked_for_s']} s" if c.get("blocked_for_s") else ""),
                     {k: v for k, v in c.items() if k not in ("_id",)}))
    return out


async def P4(db, cfg):
    out = []
    try:
        du = shutil.disk_usage("/")
        pct = du.used * 100.0 / du.total
        if pct >= th(cfg, "P4", "disk_pct", 85):
            sev = "critical" if pct >= th(cfg, "P4", "disk_critical_pct", 95) else "medium"
            out.append(f("P4", "disk", sev, "platform", f"disk {pct:.0f}% used", {"disk_pct": round(pct, 1), "free_gb": round(du.free / 2**30, 1)}))
        with open("/proc/meminfo") as fh:
            mem = {ln.split(":")[0]: int(ln.split()[1]) for ln in fh if ":" in ln}
        ram = 100.0 - mem["MemAvailable"] * 100.0 / mem["MemTotal"]
        if ram >= th(cfg, "P4", "ram_pct", 90):
            out.append(f("P4", "ram", "medium", "platform", f"RAM {ram:.0f}% used", {"ram_pct": round(ram, 1)}))
    except (OSError, KeyError, ZeroDivisionError):
        pass
    return out


async def P5(db, cfg):
    from silent_failures import swallow_counters
    lim = th(cfg, "P5", "repeats_15m", 20)
    out = []
    counters = dict(swallow_counters() or {})
    # S13 — the API/worker processes persist their counters; the security worker reads them
    async for row in db.security_signals.find({"kind": "swallow_counters", "at": {"$gte": iso_ago(minutes=15)}}).limit(50):
        for key, n in (row.get("counters") or {}).items():
            counters[key] = max(int(counters.get(key, 0)), int(n))
    if not counters and not swallow_counters():
        out.append(f("P5", "no_data", "low", "platform", "no swallowed-exception counters published by any process in 15 min", {"no_data": True}))
    for key, n in counters.items():
        if int(n) >= lim:
            out.append(f("P5", f"group:{key}", "medium", "platform", f"swallowed exception group {key} hit {n} times", {"group": key, "count": int(n)}))
    return out


# ── trading health ──────────────────────────────────────────────────────────
async def T1(db, cfg):
    q = {"mode": {"$ne": "paper"}, "trading_enabled": True, "last_heartbeat": {"$ne": None}}
    total = await db.accounts.count_documents(q)
    if total == 0:
        return []
    stale = await db.accounts.count_documents({**q, "last_heartbeat": {"$lt": iso_ago(minutes=3)}})
    if stale >= th(cfg, "T1", "stale_accounts_min", 3) and stale / total >= th(cfg, "T1", "stale_fraction", 0.5):
        return [f("T1", "platform", "high", "trading", f"{stale} of {total} live EA terminals silent for 3+ min at once", {"stale": stale, "total": total})]
    return []


async def T2(db, cfg):
    out = []
    async for a in db.ops_alerts.find({"acked_at": None, "kind": {"$in": ["unprotected_positions", "reconciliation_stuck"]}}).limit(50):
        out.append(f("T2", f"{a['kind']}:{a.get('dedup_key')}", "high", "trading", a.get("message") or a["kind"], {"kind": a["kind"]}))
    cutoff = iso_ago(seconds=th(cfg, "T2", "unacked_s", 120))
    n = await db.trades.count_documents({"status": "pending", "mt5_ticket": None, "_dispatched_at": {"$ne": None, "$lt": cutoff}})
    if n:
        out.append(f("T2", "unacked_orders", "high", "trading", f"{n} order(s) dispatched over 120 s ago and not acknowledged", {"count": n}))
    return out
