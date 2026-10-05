"""Access checks A1–A7."""
from security_agent.checks.common import events, f, iso_ago, th, window_counts

AREA = "access"


async def A1(db, cfg):
    out, by_ip, by_acc = [], {}, {}
    for r in await window_counts(db, "login", 10):          # identifier = "<ip>:<email>"
        ip, _, email = r["identifier"].partition(":")
        by_ip[ip] = by_ip.get(ip, 0) + r["n"]
        if email:
            by_acc.setdefault(email, {"n": 0, "ips": set()})
            by_acc[email]["n"] += r["n"]
            by_acc[email]["ips"].add(ip)
    for ip, n in by_ip.items():
        if n >= th(cfg, "A1", "ip_fail_5m", 20):
            out.append(f("A1", f"ip:{ip}", "high", AREA, f"{n} failed logins from {ip} in 10 min", {"ip": ip, "failures": n}))
    for email, d in by_acc.items():
        if d["n"] >= th(cfg, "A1", "account_fail_10m", 50):
            out.append(f("A1", f"account:{email}", "high", AREA, f"{d['n']} failed logins for {email} from {len(d['ips'])} IPs in 10 min",
                         {"account": email, "failures": d["n"], "ips": sorted(d["ips"])[:20]}))
    return out


async def A2(db, cfg):
    out = []
    for scope in ("2fa", "email_otp", "degraded_otp_verify"):
        for r in await window_counts(db, scope, 10):
            if r["n"] >= th(cfg, "A2", "otp_fail_10m", 10):
                out.append(f("A2", f"{scope}:{r['identifier']}", "high", AREA,
                             f"{r['n']} wrong {scope} codes for {r['identifier']} in 10 min", {"scope": scope, "target": r["identifier"], "failures": r["n"]}))
    return out


async def A3(db, cfg):
    accounts_by_ip: dict = {}
    for r in await window_counts(db, "login", 10):
        ip, _, email = r["identifier"].partition(":")
        if email:
            accounts_by_ip.setdefault(ip, set()).add(email)
    lim = th(cfg, "A3", "accounts_per_ip_10m", 5)
    return [f("A3", f"ip:{ip}", "high", AREA, f"{len(accs)} different accounts failing from {ip} in 10 min",
              {"ip": ip, "accounts": len(accs)}) for ip, accs in accounts_by_ip.items() if len(accs) >= lim]


async def A4(db, cfg):
    return [f("A4", f"user:{e['detail'].get('user_id')}:{e['at'][:16]}", "critical", AREA,
              f"refresh-token reuse for user {e['detail'].get('user_id')} (family revoked)", {"event": e["detail"], "ip": e.get("ip")})
            for e in await events(db, "refresh_token_reuse")]


async def A5(db, cfg):
    out = []
    since = iso_ago(minutes=th(cfg, "A5", "window_min", 5))
    async for a in db.login_alerts.find({"at": {"$gte": since}, "new_ip": True}).limit(200):
        fails = await db.rate_limits.count_documents({"_id": {"$regex": f"^login:[^:]+:{a.get('email', '§')}:"}})
        if fails:
            out.append(f("A5", f"user:{a.get('email')}", "medium", AREA,
                         f"new-IP login for {a.get('email')} from {a.get('country') or a.get('ip')} right after {fails} failure window(s)",
                         {"email": a.get("email"), "ip": a.get("ip"), "country": a.get("country")}))
    return out


async def A6(db, cfg):
    return [f("A6", f"actor:{e['detail'].get('user_id')}:{e['detail'].get('action')}", "critical", AREA,
              f"admin action {e['detail'].get('action')} by {e['detail'].get('user_id')} without valid step-up", {"event": e["detail"], "ip": e.get("ip")})
            for e in await events(db, "step_up_missing")]


async def A7(db, cfg):
    try:
        import ops_metrics
        s = ops_metrics.summary()
    except Exception:  # noqa: BLE001
        return []
    denied = int(s.get("count_401", 0) or 0) + int(s.get("count_403", 0) or 0) + int(s.get("count_429", 0) or 0)
    base = float(s.get("denied_baseline") or 0)
    if denied >= th(cfg, "A7", "min_count", 50) and (base <= 0 or denied >= base * th(cfg, "A7", "spike_ratio", 3.0)):
        return [f("A7", "platform", "medium", AREA, f"{denied} 401/403/429 responses in the window (baseline {base:.0f})", {"denied": denied, "baseline": base})]
    return []
