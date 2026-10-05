"""Bridge/EA (B1–B3), Secrets (S1–S5), Dependencies (D1–D2)."""
import asyncio
import os
import socket
import ssl
from datetime import datetime, timezone

from security_agent.checks.common import events, f, iso_ago, th
from security_agent import redact


# ── bridge ──────────────────────────────────────────────────────────────────
async def B1(db, cfg):
    out = []
    since = iso_ago(seconds=60)
    async for a in db.accounts.find({"hb_sightings.at": {"$gte": since}}, {"hb_sightings": 1, "label": 1}).limit(2000):
        recent = [s for s in (a.get("hb_sightings") or []) if s.get("at", "") >= since]
        sources = {(s.get("ip"), s.get("terminal")) for s in recent}
        if len(sources) >= th(cfg, "B1", "distinct_sources_60s", 2) and len(recent) >= th(cfg, "B1", "repeats", 3):
            out.append(f("B1", f"account:{a['_id']}", "critical", "bridge",
                         f"bridge token of account {a.get('label') or a['_id']} heartbeating from {len(sources)} sources within 60 s",
                         {"account_id": str(a["_id"]), "sources": [list(s) for s in sources][:10]}))
    return out


async def B2(db, cfg):
    by_ip: dict = {}
    for e in await events(db, "bridge_invalid_token", minutes=5):
        by_ip[e.get("ip") or "?"] = by_ip.get(e.get("ip") or "?", 0) + 1
    lim = th(cfg, "B2", "invalid_token_5m", 30)
    return [f("B2", f"ip:{ip}", "high", "bridge", f"{n} bridge requests with invalid tokens from {ip} in 5 min", {"ip": ip, "requests": n})
            for ip, n in by_ip.items() if n >= lim]


async def B3(db, cfg):
    return [f("B3", f"account:{e['detail'].get('account_id')}:{e['at'][:16]}", "critical", "bridge",
              f"order/setting change on account {e['detail'].get('account_id')} without valid authorisation ({e['detail'].get('reason')})",
              {"event": e["detail"], "ip": e.get("ip")}) for e in await events(db, "order_auth_invalid")]


# ── secrets ─────────────────────────────────────────────────────────────────
async def S1(db, cfg):
    hits = redact.hits(reset=True)
    out = []
    for name, n in hits.items():
        out.append(f("S1", f"pattern:{name}", "high", "secrets", f"{n} log line(s) matched the {name} secret pattern (masked)", {"pattern": name, "hits": n}))
    for e in await events(db, "log_secret_hit", minutes=5):
        out.append(f("S1", f"pattern:{e['detail'].get('pattern')}", "high", "secrets", "secret pattern hit reported by another process", {"event": e["detail"]}))
    return out


_WEAK = {"changeme", "secret", "password", "admin", "test", "dev", "default", "123456"}


async def S2(db, cfg):
    env = os.environ
    out = []
    for var in ("JWT_SECRET", "JWT_SECRET_KEY", "SECRET_KEY", "AGENT_SIGNING_KEY", "METRICS_TOKEN", "STEP_UP_BYPASS_TOKEN"):
        v = env.get(var)
        if v is not None and (len(v) < 24 or v.lower() in _WEAK):
            out.append(f("S2", f"weak:{var}", "high", "secrets", f"{var} is set but weak (length {len(v)}) — value not recorded", {"var": var, "length": len(v)}))
    for var in ("DEBUG", "FASTAPI_DEBUG", "ALLOW_INSECURE"):
        if env.get(var, "").lower() in ("1", "true", "yes"):
            out.append(f("S2", f"debug:{var}", "high", "secrets", f"debug flag {var} is enabled", {"var": var}))
    cors = env.get("CORS_ORIGINS", "")
    if cors.strip() == "*" and env.get("APP_ENV", "").lower() in ("production", "prod"):
        out.append(f("S2", "cors:wildcard", "high", "secrets", "CORS_ORIGINS is '*' in production", {"var": "CORS_ORIGINS"}))
    return out


def _public_host() -> str | None:
    url = os.environ.get("PUBLIC_BASE_URL") or os.environ.get("FRONTEND_URL") or (os.environ.get("CORS_ORIGINS") or "").split(",")[0].strip()
    if not url.startswith("https://"):
        return None
    return url[8:].split("/")[0]


async def S3(db, cfg):
    host = _public_host()
    if not host:
        return []
    def _expiry():
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=5) as sock, ctx.wrap_socket(sock, server_hostname=host) as s:
            return s.getpeercert().get("notAfter")
    try:
        exp = await asyncio.to_thread(_expiry)      # blocking socket work off the event loop
        days = (datetime.strptime(exp, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).days
    except Exception as e:  # noqa: BLE001
        return [f("S3", f"host:{host}", "medium", "secrets", f"TLS check for {host} failed: {type(e).__name__}", {"host": host})]
    if days < th(cfg, "S3", "warn_days", 14):
        sev = "critical" if days < th(cfg, "S3", "critical_days", 3) else "medium"
        return [f("S3", f"host:{host}", sev, "secrets", f"TLS certificate for {host} expires in {days} day(s)", {"host": host, "days": days})]
    return []


async def S4(db, cfg):
    host = os.environ.get("PUBLIC_IP") or _public_host()
    if not host:
        return []
    def _open(port):
        try:
            with socket.create_connection((host, port), timeout=3):
                return True
        except OSError:
            return False
    out = []
    for port, what in ((27017, "MongoDB"), (8001, "backend")):
        if await asyncio.to_thread(_open, port):
            out.append(f("S4", f"port:{port}", "critical", "secrets", f"{what} port {port} is reachable on {host} from the internet", {"host": host, "port": port}))
    return out


async def S5(db, cfg):
    host = _public_host()
    if not host:
        return []
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"https://{host}/api/health")
    except Exception:  # noqa: BLE001
        return []
    missing = [h for h in ("strict-transport-security", "content-security-policy", "x-frame-options") if h not in r.headers]
    return [f("S5", f"host:{host}", "low", "secrets", f"missing security headers on {host}: {', '.join(missing)}", {"missing": missing})] if missing else []


# ── dependencies (read stored scan results; the scans run in CI / a daily job) ──
async def _dep(db, check_id, kind, area_label):
    doc = await db.platform_state.find_one({"_id": f"dependency_audit_{kind}"})
    out = []
    for v in (doc or {}).get("vulnerabilities") or []:
        score = float(v.get("score") or 0)
        sev = "critical" if score >= 9 else "high" if score >= 7 else "medium" if score >= 4 else "low"
        out.append(f(check_id, f"{v.get('package')}:{v.get('id')}", sev, "dependencies",
                     f"{area_label} package {v.get('package')} {v.get('version')} has {v.get('id')} (fix: {v.get('fix') or 'none'})", v))
    return out


async def D1(db, cfg):
    return await _dep(db, "D1", "python", "Python")


async def D2(db, cfg):
    return await _dep(db, "D2", "frontend", "frontend")
