from dotenv import load_dotenv
from pathlib import Path
load_dotenv(Path(__file__).parent / ".env")
from secrets_loader import resolve_file_secrets
resolve_file_secrets()

import os
import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from starlette.middleware.cors import CORSMiddleware
from bson import ObjectId

from database import close_client, get_db
from app_env import is_production
from seed import seed_admin, ensure_indexes
from auth import decode_token
from ws_manager import manager as ws_manager
import bot_runner
import trade_manager
import warmer

# Routers
from routes.admin_routes import router as admin_router
from routes.migration_routes import router as migration_router
from routes.setup_routes import router as setup_router
from routes.auth_routes import router as auth_router
from routes.webauthn_routes import router as webauthn_router
from modules.pamm.api import router as pamm_router
from routes.attribution_routes import router as attribution_router
from routes.authority_routes import router as authority_router
from routes.verdict_routes import router as verdict_router
from routes.latency_routes import router as latency_router
from routes.brain_routes import router as brain_router
from routes.certification_routes import router as certification_router
from routes.soak_routes import router as soak_router
from routes.command_center_routes import router as command_center_router
from routes.certification_center_routes import router as certification_center_router
from routes.connect_routes import router as connect_router
from routes.tutorial_routes import router as tutorials_router
from routes.intent_routes import router as intent_router
from services.broker_gateway.mock_broker import router as mockbroker_router
from routes.market_routes import router as market_router
from routes.bot_routes import router as bot_router
from routes.signal_routes import router as signal_router
from routes.account_routes import router as account_router
from routes.trade_routes import router as trade_router
from routes.bridge_routes import router as bridge_router
from routes.sentiment_routes import router as sentiment_router
from routes.calendar_routes import router as calendar_router
from routes.panic_routes import router as panic_router
from routes.nl_routes import router as nl_router
from routes.copilot_routes import router as copilot_router
from routes.bugs_routes import router as bugs_router
from routes.subscription_routes import router as subscription_router
from routes.affiliate_routes import router as affiliate_router
from routes.support_routes import router as support_router
from routes.portal_routes import router as portal_router
from routes.ops_console import router as ops_console_router
from routes.broker_registry_routes import router as broker_registry_router
from routes.notification_routes import router as notification_router
from routes.telegram_routes import router as telegram_router
from routes.posture_routes import router as posture_router
from routes.rl_routes import router as rl_router
from routes.ml_routes import router as ml_router
from routes.architecture_routes import router as architecture_router
from routes.analytics_routes import router as analytics_router
from routes.entitlement_routes import router as entitlement_router
from routes.agent_routes import router as agent_router
from routes.partner_routes import router as partner_router
from routes.integrity_routes import router as integrity_router
from routes.diagnostic_routes import router as diagnostic_router
from routes.safety_blocks_routes import router as safety_blocks_router
from routes.macro_routes import router as macro_router
from routes.strategies_routes import router as strategies_router
from routes.portfolio_routes import router as portfolio_router
from routes.performance_routes import router as performance_router, public_router as performance_public_router
from routes.trace_routes import router as trace_router
from routes.execution_intel_routes import router as execution_intel_router
from routes.research_routes import router as research_router
from routes.crypto_routes import router as crypto_router
from routes.data_freshness_routes import router as data_freshness_router
from routes.shadow_routes import router as shadow_router
from routes.quant_routes import router as quant_router
from routes.scalp_routes import router as scalp_router
from routes.enterprise_routes import mgmt_router as api_keys_router, public_router as enterprise_v1_router
from routes.postmortem_routes import router as postmortem_router
from routes.auto_heal_routes import router as auto_heal_router
from routes.preferences_routes import router as preferences_router
from routes.insights_routes import router as insights_router
from routes.optimizer_routes import router as optimizer_router

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("trading-bot")

# iter-158 — correlation ids on every log record (API + workers)
from correlation import install as _install_correlation, set_correlation_id
_install_correlation()

from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(_app):
    # on_startup / on_shutdown are defined later in this module — names
    # resolve at call time, after the module has fully loaded.
    _extend_uvicorn_keepalive()
    await on_startup()
    yield
    await on_shutdown()


def _extend_uvicorn_keepalive():
    """Fix intermittent Cloudflare 520s in production: uvicorn's default
    5s keep-alive closes idle connections faster than the upstream proxies
    (GFE/Cloudflare hold backend connections ~600s), so a request can land
    on a just-closed socket → connection reset → 520 'unparseable response'.
    The launch command isn't ours to change in the deploy environment, so
    raise the running server's Config in-process (per GCP LB guidance the
    backend timeout must exceed 600s; default 650)."""
    try:
        import gc
        import uvicorn
        target = int(os.environ.get("UVICORN_KEEPALIVE_SECONDS", "650"))
        for obj in gc.get_objects():
            if isinstance(obj, uvicorn.Config) and \
                    obj.timeout_keep_alive < target:
                obj.timeout_keep_alive = target
                logging.getLogger("server").info(
                    "uvicorn timeout_keep_alive raised to %ss", target)
    except Exception:  # noqa: BLE001 — never block startup for this
        logging.getLogger("server").warning(
            "could not extend uvicorn keep-alive", exc_info=True)

app = FastAPI(title="AI Trading Bot API", version="1.1.0", lifespan=_lifespan)


# Root-level liveness probe. The Kubernetes/deployment health check hits
# GET /health (NO /api prefix). It must return 200 fast and WITHOUT any DB
# dependency so the pod becomes ready even while startup migrations run and
# stays alive if Atlas briefly blips. Rich DB-aware checks live under
# /api/health and /api/health/ready.
@app.get("/health")
@app.get("/healthz")
async def root_health():
    return {"status": "ok"}


@app.middleware("http")
async def csrf_middleware(request, call_next):
    """CSRF double-submit enforcement for cookie-authenticated mutations
    (SameSite=None cookies make cross-site sends possible; CORS alone is
    not a CSRF defense). Bearer/bridge/webhook callers are unaffected."""
    from fastapi.responses import JSONResponse
    from security import csrf_check
    err = csrf_check(request)
    if err is not None:
        return JSONResponse(
            status_code=403,
            content={"detail": {"code": "csrf_failed", "reason": err,
                                "message": "Request blocked by CSRF "
                                           "protection. Refresh and retry."}})
    return await call_next(request)


@app.middleware("http")
async def request_id_middleware(request, call_next):
    """Iter-152 · distributed request IDs + structured JSON access logs."""
    import uuid
    import re as _re
    import time as _t
    import json as _json
    # SEC-003 — attacker-controlled ids are restricted to [A-Za-z0-9_-]
    # before they reach logs/headers (log-forging / CWE-117 defense).
    _clean = lambda v: _re.sub(r"[^A-Za-z0-9_-]", "", v or "")[:64]  # noqa: E731
    rid = _clean(request.headers.get("X-Request-ID")) or uuid.uuid4().hex[:16]
    tid = _clean(request.headers.get("X-Trace-ID")) or rid
    set_correlation_id(rid)
    from correlation import reset_log_fields, set_log_fields
    reset_log_fields()
    if tid != rid:
        set_log_fields(trace=tid)
    t0 = _t.perf_counter()
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    response.headers["X-Trace-ID"] = tid
    path = request.url.path
    if path.startswith("/api") and path not in ("/api/metrics", "/api/ws"):
        _dur_ms = round((_t.perf_counter() - t0) * 1000, 1)
        try:
            import ops_metrics
            ops_metrics.record(_dur_ms, response.status_code)
        except Exception:
            pass
        logging.getLogger("access").info(_json.dumps({
            "rid": rid, "trace": tid, "m": request.method, "p": path,
            "s": response.status_code,
            "ms": _dur_ms}))
    return response


api_router = APIRouter(prefix="/api")


@api_router.get("/")
async def root():
    return {"service": "ai-trading-bot", "status": "ok"}


_startup_error: str | None = None


@api_router.get("/health")
async def health():
    db = get_db()
    try:
        await db.command("ping")
        return {"status": "ok", "db": "connected"}
    except Exception as e:
        # Never leak connection topology / driver internals publicly.
        cid = uuid.uuid4().hex[:12]
        logger.error("health degraded [cid=%s]: %s", cid, e)
        return {"status": "degraded", "db": "unavailable", "cid": cid}


@api_router.get("/health/live")
async def health_live():
    return {"status": "ok"}


@api_router.get("/health/ready")
async def health_ready():
    """Readiness: DB ping + critical unique indexes + submission-slot
    integrity. Degraded readiness returns 503 so orchestrators stop
    routing traffic without leaking internals."""
    from fastapi.responses import JSONResponse
    checks = {}
    ok = True
    # audit r3 P0 · critical startup failures (indexes, dependency health)
    # fail readiness so orchestrators never route to a degraded replica.
    if _startup_error is not None:
        checks["startup"] = f"failed:{_startup_error}"
        ok = False
    db = get_db()
    try:
        await db.command("ping")
        checks["db"] = "ok"
    except Exception as e:
        cid = uuid.uuid4().hex[:12]
        logger.error("readiness db fail [cid=%s]: %s", cid, e)
        checks["db"] = "unavailable"
        ok = False
    if checks["db"] == "ok":
        try:
            from seed import dependency_health_check
            dep = await dependency_health_check()
            dep_ok = bool(dep.get("deps_ok") and dep.get("db_ok")
                          and dep.get("indexes_ok"))
            checks["critical_indexes"] = "ok" if dep_ok else "missing"
            ok = ok and dep_ok
        except Exception:
            checks["critical_indexes"] = "unknown"
        from scalp.engine import capacity_integrity_reason
        cap = capacity_integrity_reason()
        checks["submission_capacity"] = "ok" if cap is None else "violated"
        ok = ok and cap is None
    body = {"status": "ok" if ok else "degraded", "checks": checks}
    return body if ok else JSONResponse(status_code=503, content=body)


@api_router.get("/ea-script")
@api_router.get("/bridge/download-ea")
async def ea_script():
    path = Path(__file__).parent / "static" / "EmergentTradingBridge.mq5"
    if not path.exists():
        return {"error": "EA file missing"}
    # No-cache headers so MT5 / browsers always pull the latest version —
    # otherwise users reinstall and still get an old cached .mq5.
    return FileResponse(path, media_type="text/plain",
                        filename="EmergentTradingBridge.mq5",
                        headers={
                            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                            "Pragma": "no-cache",
                            "Expires": "0",
                        })


@api_router.get("/artifacts/{sha256}")
async def artifact_by_hash(sha256: str):
    """iter-139 — content-addressed IMMUTABLE artifact delivery. The URL *is*
    the integrity contract: the file is hashed at serve time and only
    returned if it matches the requested digest, so a mutable/compromised
    file can never be served under an old link. Manifest URLs pin these."""
    import hashlib as _hashlib
    sha256 = sha256.lower().strip()
    if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
        return JSONResponse(status_code=400, content={"error": "bad_digest"})
    static_dir = Path(__file__).parent / "static"
    # iter-160 — immutable release store: filename IS the digest
    store_hit = static_dir / "artifacts" / sha256
    candidates = ([("artifacts/" + sha256, "application/octet-stream")]
                  if store_hit.exists() else [])
    candidates += [("EmergentTradingBridge.ex5", "application/octet-stream"),
                   ("EmergentTradingBridge.mq5", "text/plain")]
    for fname, media in candidates:
        path = static_dir / fname
        if not path.exists():
            continue
        h = _hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        if h.hexdigest() == sha256:
            return FileResponse(path, media_type=media, filename=fname,
                                headers={"Cache-Control":
                                         "public, max-age=31536000, immutable",
                                         "X-Artifact-SHA256": sha256})
    return JSONResponse(status_code=404, content={"error": "artifact_not_found",
                        "detail": "no published artifact matches this digest"})


_trust_stats_cache = {"at": 0.0, "data": None}


@api_router.get("/public/trust-stats")
async def public_trust_stats():
    """Anonymous aggregate stats for the landing-page trust bar.
    No per-user data; cached in-process for 5 minutes."""
    import time as _time
    if _trust_stats_cache["data"] and _time.time() - _trust_stats_cache["at"] < 300:
        return _trust_stats_cache["data"]
    db = get_db()
    accounts = await db.accounts.count_documents({})
    vetoed = await db.signals.count_documents({"action": "HOLD"})
    # Uptime: coverage of the 30-min ops soak sampler over the last 30 days
    # (or since the first sample), clamped to 100.
    now = datetime.now(timezone.utc)
    first = await db.ops_soak_samples.find_one({}, sort=[("at", 1)],
                                               projection={"at": 1})
    uptime = None
    if first and first.get("at"):
        start = first["at"]
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        start = max(start, now - timedelta(days=30))
        expected = max(1, int((now - start).total_seconds() // 1800))
        got = await db.ops_soak_samples.count_documents(
            {"at": {"$gte": start.replace(tzinfo=None)}})
        uptime = round(min(100.0, got * 100.0 / expected), 2)
    data = {"accounts_protected": accounts, "signals_vetoed": vetoed,
            "uptime_30d_pct": uptime, "as_of": now.isoformat()}
    _trust_stats_cache.update(at=_time.time(), data=data)
    return data


@api_router.get("/ea-script.ex5")
async def ea_binary():
    """CI-built EX5 delivery (iter-125 correction #3). Installers deploy the
    exact CI-compiled binary and verify its SHA-256 against the signed
    artifact manifest — local MetaEditor recompiles are a fallback only."""
    import hashlib as _hashlib
    path = Path(__file__).parent / "static" / "EmergentTradingBridge.ex5"
    if not path.exists():
        return JSONResponse(status_code=409, content={
            "error": "ex5_not_published",
            "detail": ("No CI-built EX5 has been published to this server "
                       "yet — the signed release pipeline uploads it to "
                       "backend/static/EmergentTradingBridge.ex5. Installers "
                       "fall back to local MetaEditor compilation.")})
    digest = _hashlib.sha256(path.read_bytes()).hexdigest()
    return FileResponse(path, media_type="application/octet-stream",
                        filename="EmergentTradingBridge.ex5",
                        headers={
                            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                            "Pragma": "no-cache",
                            "X-STOIC-SHA256": digest,
                        })


@api_router.get("/setup/installer.ps1")
async def installer_script():
    """Serve the PowerShell auto-installer for MT5 hosts.

    The installer is intentionally public (no auth) — it carries no
    secrets and only becomes useful when paired with a token via
    POST /api/setup/claim-pairing. Surfaced as a top-level URL so users
    can `irm <backend>/api/setup/installer.ps1 | iex` on their VPS.
    """
    path = Path(__file__).parent / "static" / "STOIC-Installer.ps1"
    if not path.exists():
        return {"error": "Installer missing"}
    return FileResponse(path, media_type="text/plain",
                        filename="STOIC-Installer.ps1",
                        headers={
                            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                            "Pragma": "no-cache",
                        })


# Mount routers
api_router.include_router(admin_router)
api_router.include_router(migration_router)
api_router.include_router(setup_router)
api_router.include_router(auth_router)
api_router.include_router(webauthn_router)
api_router.include_router(pamm_router)
api_router.include_router(intent_router)
api_router.include_router(authority_router)
api_router.include_router(attribution_router)
api_router.include_router(verdict_router)
api_router.include_router(latency_router)
api_router.include_router(brain_router)
api_router.include_router(certification_router)
api_router.include_router(soak_router)
api_router.include_router(command_center_router)
api_router.include_router(certification_center_router)
api_router.include_router(connect_router)
api_router.include_router(tutorials_router)
from app_env import is_production as _is_production
if not _is_production():
    # SEC-003: mock broker is a certification test double — never in prod
    # (routed through is_production so APP_ENV=prod is covered too)
    api_router.include_router(mockbroker_router)
api_router.include_router(market_router)
api_router.include_router(bot_router)
api_router.include_router(signal_router)
api_router.include_router(account_router)
api_router.include_router(trade_router)
api_router.include_router(bridge_router)
api_router.include_router(sentiment_router)
api_router.include_router(calendar_router)
api_router.include_router(panic_router)
api_router.include_router(nl_router)
api_router.include_router(copilot_router)
api_router.include_router(bugs_router)
api_router.include_router(subscription_router)
api_router.include_router(affiliate_router)
api_router.include_router(support_router)
api_router.include_router(portal_router)
api_router.include_router(ops_console_router)
api_router.include_router(broker_registry_router)
api_router.include_router(notification_router)
api_router.include_router(telegram_router)
api_router.include_router(posture_router)
api_router.include_router(rl_router)
api_router.include_router(ml_router)
api_router.include_router(architecture_router)
api_router.include_router(analytics_router)
api_router.include_router(entitlement_router)
api_router.include_router(agent_router)
api_router.include_router(partner_router)
api_router.include_router(integrity_router)
api_router.include_router(diagnostic_router)
api_router.include_router(safety_blocks_router)
api_router.include_router(macro_router)
api_router.include_router(strategies_router)
api_router.include_router(portfolio_router)
api_router.include_router(performance_router)
api_router.include_router(performance_public_router)
api_router.include_router(trace_router)
api_router.include_router(execution_intel_router)
api_router.include_router(research_router)
api_router.include_router(crypto_router)
api_router.include_router(data_freshness_router)
api_router.include_router(shadow_router)
api_router.include_router(quant_router)
api_router.include_router(scalp_router)
api_router.include_router(postmortem_router)
api_router.include_router(auto_heal_router)
api_router.include_router(preferences_router)
api_router.include_router(insights_router)
api_router.include_router(optimizer_router)
api_router.include_router(api_keys_router)
api_router.include_router(enterprise_v1_router)
from routes.metrics_routes import router as metrics_router  # noqa: E402
api_router.include_router(metrics_router)
from routes.ops_routes import router as ops_router  # noqa: E402
api_router.include_router(ops_router)
from routes.validation_routes import router as validation_router  # noqa: E402
api_router.include_router(validation_router)
from routes.risk_layers_routes import router as risk_layers_router  # noqa: E402
from routes.broker_intel_routes import router as broker_intel_router  # noqa: E402
api_router.include_router(risk_layers_router)
api_router.include_router(broker_intel_router)
from routes.journal_routes import router as journal_router, public_router as journal_public_router  # noqa: E402
api_router.include_router(journal_router)
api_router.include_router(journal_public_router)
from routes.learning_routes import router as learning_router  # noqa: E402
from routes.governance_routes import router as governance_router  # noqa: E402
from routes.twin_routes import router as twin_router  # noqa: E402
from routes.genetics_routes import router as genetics_router  # noqa: E402
from routes.coach_routes import router as coach_router  # noqa: E402
from routes.marketplace_routes import router as marketplace_router  # noqa: E402
from routes.config_version_routes import router as config_version_router  # noqa: E402
from routes.liveops_routes import router as liveops_router  # noqa: E402
from routes.infra_routes import router as infra_router  # noqa: E402
api_router.include_router(learning_router)
api_router.include_router(governance_router)
api_router.include_router(twin_router)
api_router.include_router(genetics_router)
api_router.include_router(coach_router)
api_router.include_router(marketplace_router)
api_router.include_router(config_version_router)
api_router.include_router(liveops_router)
api_router.include_router(infra_router)


# ---------- WebSocket ----------
@api_router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    """Authenticated WS via the access_token cookie. Query-string tokens
    leak through proxy/LB logs and browser history — allowed only when
    WS_ALLOW_QUERY_TOKEN=true is set explicitly (non-production use)."""
    # Browser Origin must match the CORS allowlist when one is configured
    # (cross-site WS hijacking defence at the app layer).
    from security import _allowed_origins
    _allowed = _allowed_origins()
    _origin = (websocket.headers.get("origin") or "").rstrip("/")
    # Same-origin upgrades are always trusted (the Origin host equals the
    # request Host) — fixes WS 403s when the deployment URL isn't listed in
    # CORS_ORIGINS (preview forks / custom domains). Cross-origin still
    # requires an allowlist entry.
    _host = (websocket.headers.get("host") or "").strip().lower()
    _origin_host = _origin.split("://", 1)[-1].strip().lower()
    _same_origin = bool(_host) and _origin_host == _host

    async def _reject(code: int):
        # Closing BEFORE accept() surfaces as an opaque HTTP 403 handshake
        # error in browsers (the original "WS 403" bug). Accept first, then
        # close with a meaningful app-level code (4401/4403) the frontend
        # can distinguish and act on.
        try:
            await websocket.accept()
        except Exception:  # noqa: BLE001
            pass
        await websocket.close(code=code)

    if _allowed and _origin and not _same_origin \
            and _origin not in _allowed:
        await _reject(4403)
        return
    token = websocket.cookies.get("access_token")
    if not token and (os.environ.get("WS_ALLOW_QUERY_TOKEN", "false")
                      .lower() == "true"):
        token = websocket.query_params.get("token")
    if not token:
        await _reject(4401)
        return
    try:
        payload = decode_token(token)
        if payload.get("type") != "access":
            await _reject(4401)
            return
        user_id = payload["sub"]
        db = get_db()
        if not await db.users.find_one({"_id": ObjectId(user_id)}):
            await _reject(4401)
            return
    except Exception:
        await _reject(4401)
        return

    await ws_manager.connect(user_id, websocket)
    try:
        await websocket.send_json({"type": "connected", "payload": {"user_id": user_id}})
        while True:
            # Keep the socket alive; we ignore client messages but consume them
            try:
                await websocket.receive_text()
            except WebSocketDisconnect:
                break
    except WebSocketDisconnect:
        pass
    finally:
        await ws_manager.disconnect(user_id, websocket)


app.include_router(api_router)


# Belt-and-suspenders: any route that forgets to use route_utils.parse_object_id
# and crashes on a malformed Mongo ObjectId now returns a clean 404 instead of
# a 500. Prevents the same class of bug (iter22 P2.2) from re-emerging when
# new routes are added.
from bson.errors import InvalidId
from fastapi.responses import JSONResponse


@app.exception_handler(InvalidId)
async def _invalid_id_handler(_request: Request, _exc: InvalidId):
    return JSONResponse(status_code=404, content={"detail": "Resource not found"})


# Security headers (iter-155) — defense-in-depth on every API response.
# The browser-facing HTML gets its headers from the Cloudflare edge
# (Transform Rules — see docs/CLOUDFLARE_EDGE.md); these cover direct API
# access and act as a backstop if edge rules are ever misconfigured.
_SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains; preload",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), "
                          "payment=(), usb=()",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-site",
}


@app.middleware("http")
async def _security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    for k, v in _SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    return response


# CORS — credentialed cookies must NEVER be paired with a wildcard origin.
# Either: explicit allowlist with credentials, OR wildcard WITHOUT credentials.
# If CORS_ORIGINS is unset/wildcard, we strip credentials so a malicious site
# can't pull authenticated calls from a logged-in browser.
cors_origins_env = (os.environ.get("CORS_ORIGINS") or "").strip()
if cors_origins_env and cors_origins_env != "*":
    allowed = [o.strip() for o in cors_origins_env.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    # Safe wildcard: no credentials. Browsers won't send/receive cookies cross-origin.
    # API can still serve public/non-cookie traffic.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )


_bot_runner_task = None
_warmer_task = None
_trade_manager_task = None
_auto_heal_task = None
_stuck_sync_task = None
_optimizer_task = None
_nightly_tuner_task = None
_scalp_reconcile_task = None


from background_loops import (_analytics_loop, _auto_heal_loop,
                              _eod_flatten_loop, _mode_guardian_loop,
                              _model_maintenance_loop,
                              _nightly_tuning_loop, _optimizer_loop,
                              _pamm_sweep_loop,
                              _protection_guard_loop, _scalp_reconcile_loop,
                              _soak_sampler_loop, _stuck_open_sync_loop,
                              _billing_loop)
_protection_task = None
_analytics_task = None
_model_maint_task = None
_ops_alert_task = None
_portfolio_stop_task = None
_scheduled_drills_task = None


async def on_startup():
    global _bot_runner_task, _warmer_task, _trade_manager_task, _auto_heal_task, _stuck_sync_task, _optimizer_task, _nightly_tuner_task, _scalp_reconcile_task
    # review item 5 / iter-182 — Origin enforcement is AUTOMATIC in
    # production (no CSRF_ENFORCE_ORIGIN secret needed) and localhost /
    # preview entries are filtered out of CORS_ORIGINS automatically.
    if is_production():
        from security import _allowed_origins
        if not _allowed_origins():
            raise RuntimeError(
                "APP_ENV=production requires CORS_ORIGINS to contain at "
                "least one real production origin (localhost/preview "
                "entries are ignored in production).")
        # SEC-001 — test bypass secrets must never exist in production.
        if (os.environ.get("STEP_UP_BYPASS_TOKEN")
                or os.environ.get("RATE_LIMIT_BYPASS_TOKEN")):
            raise RuntimeError(
                "APP_ENV=production forbids STEP_UP_BYPASS_TOKEN / "
                "RATE_LIMIT_BYPASS_TOKEN — unset them before deploying.")
        # iter-136 — admin MFA enforcement and release signing are mandatory
        # in production.
        if os.environ.get("ADMIN_MFA_ENFORCED", "true").lower() != "true":
            raise RuntimeError(
                "APP_ENV=production forbids ADMIN_MFA_ENFORCED=false — "
                "admin accounts must enroll TOTP 2FA.")
        if not os.environ.get("ED25519_SIGNING_KEY_B64"):
            raise RuntimeError(
                "APP_ENV=production requires ED25519_SIGNING_KEY_B64 for "
                "release manifest signing.")
    # iter-183 — runtime watchdog & crash forensics (RSS, loop-blockage
    # stacks, restart history) — must start before anything heavy.
    from runtime_watchdog import start_watchdog
    start_watchdog()
    try:
        await ensure_indexes()
        from modules.pamm.models import ensure_pamm_setup
        await ensure_pamm_setup(get_db())
        await seed_admin()
        # Safety review — DEFAULT_MODE is observe; grandfather migration
        # stamps pre-existing configs explicitly (idempotent, audited).
        from operational_modes import (migrate_default_modes,
                                       remigrate_autonomous_to_supervised)
        await migrate_default_modes(get_db())
        from broker_registry import ensure_seed as _seed_brokers
        await _seed_brokers(get_db())
        await remigrate_autonomous_to_supervised(get_db())
        # Correction #4 — converge any config change journaled mid-crash.
        from config_promotion import repair_incomplete_promotions
        await repair_incomplete_promotions(get_db())
        # iter-125 — identity structure backfill (display_name /
        # expected_identity / verified_identity). Idempotent.
        from identity_model import backfill_identity_structure
        await backfill_identity_structure(get_db())
        from seed import dependency_health_check
        await dependency_health_check()
        logger.info("Startup: indexes ensured, admin seeded.")
        # audit P0 · worker mode must be an EXPLICIT choice. Unset in a
        # multi-replica production API would silently double-run trading
        # loops, so the default is now OFF with a critical warning.
        worker_mode = os.environ.get("BACKGROUND_WORKERS_IN_PROCESS")
        if worker_mode is None:
            logger.critical(
                "BACKGROUND_WORKERS_IN_PROCESS is NOT SET — embedded "
                "background loops are DISABLED (fail-safe default). Set it "
                "to 'true' for single-process mode or run the dedicated "
                "workers (python -m workers.*) with it set to 'false'.")
            return
        if worker_mode.lower() != "true":
            logger.info("Background loops NOT started in-process "
                        "(external workers mode).")
            return
        _bot_runner_task = asyncio.create_task(bot_runner.loop())
        _warmer_task = asyncio.create_task(warmer.loop())
        _trade_manager_task = asyncio.create_task(trade_manager.run_loop())
        _auto_heal_task = asyncio.create_task(_auto_heal_loop())
        _stuck_sync_task = asyncio.create_task(_stuck_open_sync_loop())
        _optimizer_task = asyncio.create_task(_optimizer_loop())
        _nightly_tuner_task = asyncio.create_task(_nightly_tuning_loop())
        _scalp_reconcile_task = asyncio.create_task(_scalp_reconcile_loop())
        _eod_flatten_task = asyncio.create_task(_eod_flatten_loop())
        asyncio.create_task(_soak_sampler_loop())
        asyncio.create_task(_billing_loop())
        asyncio.create_task(_mode_guardian_loop())
        asyncio.create_task(_pamm_sweep_loop())
        # Phase F — separated services (in-process mode runs them all)
        global _protection_task, _analytics_task, _model_maint_task
        _protection_task = asyncio.create_task(_protection_guard_loop())
        _analytics_task = asyncio.create_task(_analytics_loop())
        _model_maint_task = asyncio.create_task(_model_maintenance_loop())
        from alerting import _ops_alert_loop
        global _ops_alert_task
        _ops_alert_task = asyncio.create_task(_ops_alert_loop())
        from risk_layers import _portfolio_stop_loop
        global _portfolio_stop_task
        _portfolio_stop_task = asyncio.create_task(_portfolio_stop_loop())
        from broker_intel import _broker_intel_loop
        global _broker_intel_task
        _broker_intel_task = asyncio.create_task(_broker_intel_loop())
        from scheduled_drills import scheduled_drill_loop
        global _scheduled_drills_task
        _scheduled_drills_task = asyncio.create_task(scheduled_drill_loop())
        logger.info("Bot runner + warmer + trade manager + auto-heal + stuck-sync + optimizer + nightly-tuner scheduled.")
    except Exception as e:
        logger.exception("Startup error: %s", e)
        # audit r3 P0 · a failed startup must not serve silently: readiness
        # reports it (503) and production terminates outright.
        global _startup_error
        _startup_error = f"{type(e).__name__}"
        if is_production():
            raise


async def on_shutdown():
    for task in (_bot_runner_task, _warmer_task, _trade_manager_task,
                 _auto_heal_task, _stuck_sync_task, _optimizer_task,
                 _nightly_tuner_task, _scalp_reconcile_task,
                 _protection_task, _analytics_task, _model_maint_task,
                 _ops_alert_task, _portfolio_stop_task,
                 _scheduled_drills_task):
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
    await close_client()

