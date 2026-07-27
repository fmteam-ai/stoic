"""iter-165 — re-audit remediations: APP_ENV prod-flag normalization
(SEC-001) and host-agent release-control scoping + corroboration (SEC-002)."""
import os
import sys
from datetime import datetime, timezone

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)
from dotenv import load_dotenv

load_dotenv(os.path.join(_BACKEND_DIR, ".env"))


def _run(coro):
    from conftest import run_async
    return run_async(coro)


def _db():
    from database import get_db
    return get_db()


# ─── SEC-001: APP_ENV normalization ──────────────────────────────────
def test_is_production_treats_prod_as_production(monkeypatch):
    from app_env import is_production
    for val, expected in (("production", True), ("prod", True),
                          ("PROD", True), (" Production ", True),
                          ("", False), ("preview", False),
                          ("staging", False)):
        monkeypatch.setenv("APP_ENV", val)
        assert is_production() is expected, val
    monkeypatch.delenv("APP_ENV", raising=False)
    assert is_production() is False


def test_step_up_bypass_refused_under_prod_shorthand(monkeypatch):
    """The CI step-up bypass must be refused when APP_ENV=prod (not only the
    exact string 'production')."""
    from bson import ObjectId

    import step_up
    db = _db()
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("STEP_UP_BYPASS_TOKEN", "iter165-bypass")

    class _Req:
        headers = {"X-Step-Up-Bypass": "iter165-bypass"}

    async def scenario():
        u = await db.users.find_one({"role": "admin"}, {"_id": 1})
        uid = str(u["_id"])
        # 2FA-less admin under prod shorthand: bypass ignored → blocked
        await db.users.update_one({"_id": ObjectId(uid)},
                                  {"$set": {"two_factor_enabled": False}})
        try:
            await step_up.require_step_up(db, {"id": uid}, _Req(),
                                          "release_promote")
            return "allowed"
        except Exception as e:  # HTTPException
            return getattr(e, "detail", {})
    detail = _run(scenario())
    assert isinstance(detail, dict), f"bypass was honored under prod: {detail}"
    assert detail.get("code") in ("mfa_enrollment_required",
                                  "step_up_required")


def test_rate_limit_bypass_refused_under_prod_shorthand(monkeypatch):
    import security
    db = _db()
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("RATE_LIMIT_BYPASS_TOKEN", "iter165-rl")

    class _Req:
        headers = {"x-ratelimit-bypass": "iter165-rl"}

    async def scenario():
        scope = f"iter165-{os.urandom(3).hex()}"
        ident = "1.2.3.4"
        raised = False
        try:
            for _ in range(6):
                await security.rate_limit(db, scope, ident, max_attempts=3,
                                          window_sec=60, request=_Req())
        except Exception:
            raised = True
        await db.rate_limits.delete_many({"_id": {"$regex": scope}})
        return raised
    assert _run(scenario()) is True, "rate-limit bypass honored under prod"


# ─── SEC-002: host-agent deploy-status scoping + corroboration ────────
def test_unassigned_agent_failure_does_not_raise_fleet_alert():
    """A deploy failure for an artifact the agent was never assigned is
    recorded as an anomaly (warning), NOT a fleet deployment_failed."""
    from routes.infra_routes import agent_deploy_status_ep
    db = _db()
    tok = f"iter165-tok-{os.urandom(6).hex()}"
    aid = f"iter165-agent-{os.urandom(4).hex()}"
    bogus = "f" * 64

    async def scenario():
        await db.vps_agents.insert_one(
            {"agent_id": aid, "agent_token": tok, "revoked": False})
        try:
            await agent_deploy_status_ep(
                {"agent_token": tok, "ok": False, "artifact": "evil.msi",
                 "sha256": bogus, "detail": "forced"})
            fleet = await db.ops_alerts.count_documents(
                {"kind": "deployment_failed", "meta.sha256": bogus})
            anom = await db.ops_alerts.count_documents(
                {"kind": "agent_deploy_anomaly", "meta.agent_id": aid})
            return fleet, anom
        finally:
            await db.vps_agents.delete_many({"agent_id": aid})
            await db.ops_alerts.delete_many(
                {"dedup_key": {"$regex": aid}})
            await db.agent_deployments.delete_many({"agent_id": aid})
    fleet, anom = _run(scenario())
    assert fleet == 0, "unassigned artifact failure raised a fleet alert"
    assert anom == 1, "anomaly alert not recorded"


def test_single_agent_failure_below_corroboration_threshold():
    """distinct_fail_agents must not reach the auto-rollback threshold from a
    single agent's report."""
    from deployment_health import MIN_FAIL_AGENTS, distinct_fail_agents
    db = _db()
    now = datetime.now(timezone.utc)
    tag = f"iter165-{os.urandom(3).hex()}"

    async def scenario():
        await db.ops_alerts.insert_one(
            {"kind": "deployment_failed", "severity": "critical",
             "dedup_key": f"{tag}-solo", "meta": {"agent_id": f"{tag}-only"},
             "acked_at": None, "created_at": now})
        try:
            return await distinct_fail_agents(db, now)
        finally:
            await db.ops_alerts.delete_many({"dedup_key": f"{tag}-solo"})
    n = _run(scenario())
    assert n == 1
    assert n < MIN_FAIL_AGENTS, "threshold too low to resist a single agent"
