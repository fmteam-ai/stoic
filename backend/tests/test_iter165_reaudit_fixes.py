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
    monkeypatch.setenv("STEP_UP_BYPASS_TOKEN", "dummy-fixture-value-a")

    class _Req:
        headers = {"X-Step-Up-Bypass": "dummy-fixture-value-a"}

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
    monkeypatch.setenv("RATE_LIMIT_BYPASS_TOKEN", "dummy-fixture-value-b")

    class _Req:
        headers = {"x-ratelimit-bypass": "dummy-fixture-value-b"}

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
    """distinct_fail_tenants must not reach the auto-rollback threshold from a
    single tenant, no matter how many agents that tenant spins up."""
    from deployment_health import (MIN_FAIL_TENANTS, distinct_fail_tenants)
    db = _db()
    now = datetime.now(timezone.utc)
    tag = f"iter165-{os.urandom(3).hex()}"
    sha = "c" * 64

    async def scenario():
        # ONE tenant, MANY agents, all reporting failure for the same digest.
        await db.ops_alerts.insert_many([
            {"kind": "deployment_failed", "severity": "critical",
             "dedup_key": f"{tag}-{i}",
             "meta": {"agent_id": f"{tag}-agent{i}", "user_id": f"{tag}-tenant",
                      "sha256": sha},
             "acked_at": None, "created_at": now} for i in range(5)])
        try:
            scoped = await distinct_fail_tenants(db, now, {sha})
            unscoped = await distinct_fail_tenants(db, now)
            return scoped, unscoped
        finally:
            await db.ops_alerts.delete_many(
                {"dedup_key": {"$regex": tag}})
    scoped, unscoped = _run(scenario())
    assert scoped == 1, "5 agents of one tenant must count as ONE tenant"
    assert unscoped == 1
    assert scoped < MIN_FAIL_TENANTS, "one tenant must not reach the threshold"


def test_two_distinct_tenants_reach_threshold():
    from deployment_health import (MIN_FAIL_TENANTS, distinct_fail_tenants)
    db = _db()
    now = datetime.now(timezone.utc)
    tag = f"iter165-{os.urandom(3).hex()}"
    sha = "d" * 64

    async def scenario():
        await db.ops_alerts.insert_many([
            {"kind": "deployment_failed", "severity": "critical",
             "dedup_key": f"{tag}-{t}",
             "meta": {"agent_id": f"{tag}-a{t}", "user_id": f"{tag}-tenant{t}",
                      "sha256": sha},
             "acked_at": None, "created_at": now} for t in range(2)])
        try:
            return await distinct_fail_tenants(db, now, {sha})
        finally:
            await db.ops_alerts.delete_many({"dedup_key": {"$regex": tag}})
    n = _run(scenario())
    assert n >= MIN_FAIL_TENANTS


def test_digest_scoping_ignores_other_releases():
    """Failures for a DIFFERENT digest than the one under evaluation must not
    count toward corroboration."""
    from deployment_health import distinct_fail_tenants
    db = _db()
    now = datetime.now(timezone.utc)
    tag = f"iter165-{os.urandom(3).hex()}"

    async def scenario():
        await db.ops_alerts.insert_many([
            {"kind": "deployment_failed", "severity": "critical",
             "dedup_key": f"{tag}-{t}",
             "meta": {"user_id": f"{tag}-tenant{t}", "sha256": "e" * 64},
             "acked_at": None, "created_at": now} for t in range(3)])
        try:
            return await distinct_fail_tenants(db, now, {"f" * 64})
        finally:
            await db.ops_alerts.delete_many({"dedup_key": {"$regex": tag}})
    assert _run(scenario()) == 0


def test_agent_registration_quota_per_tenant(monkeypatch):
    from vps_agent import create_bootstrap_token, register_agent
    db = _db()
    monkeypatch.setenv("VPS_MAX_AGENTS_PER_USER", "2")
    uid = f"iter165-quota-{os.urandom(4).hex()}"

    async def scenario():
        try:
            for _ in range(2):
                bt = await create_bootstrap_token(db, uid, f"dep-{uid}")
                await register_agent(db, bt["token"],
                                     {"machine_fingerprint": os.urandom(4).hex()})
            # 3rd registration must be refused by the quota
            bt = await create_bootstrap_token(db, uid, f"dep-{uid}")
            try:
                await register_agent(db, bt["token"],
                                     {"machine_fingerprint": "x"})
                return "allowed"
            except ValueError as e:
                return str(e)
        finally:
            await db.vps_agents.delete_many({"user_id": uid})
            await db.vps_bootstrap_tokens.delete_many({"user_id": uid})
    out = _run(scenario())
    assert "limit reached" in out, out
