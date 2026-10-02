"""Ops/platform hardening: ops token separation (METRICS_TOKEN read-only,
OPS_DEPLOY_TOKEN for mutating machine calls), verified-admin checks,
scalp retrain admin/single-flight/off-loop, supervised task registry,
event-loop lag metric, in-process workers under leader leases, nginx
real-IP chain."""
import ast
import asyncio
import os
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

pytestmark = pytest.mark.unit

BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(BACKEND)

MT, DT = "metrics-tok-AAAA", "deploy-tok-BBBB"


def _req(method="GET", headers=None, path="/api/ops/x"):
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": method, "path": path,
                    "headers": raw, "query_string": b"", "client": ("1.2.3.4", 1)})


@pytest.fixture
def ops_env(monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", MT)
    monkeypatch.setenv("OPS_DEPLOY_TOKEN", DT)
    monkeypatch.delenv("OPS_ALLOW_METRICS_TOKEN_FOR_DEPLOY", raising=False)
    monkeypatch.setenv("ADMIN_MFA_ENFORCED", "true")
    import auth

    async def no_session(_request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    monkeypatch.setattr(auth, "get_current_user", no_session)
    import routes.ops_routes as ops
    return ops


def _run(coro):
    return asyncio.run(coro)


# ── 1 · METRICS_TOKEN vs OPS_DEPLOY_TOKEN ────────────────────────────────────
def test_metrics_token_is_read_only(ops_env):
    ops = ops_env
    m = {"X-Metrics-Token": MT}
    assert _run(ops._ops_actor(_req("GET", m))) == (True, "metrics-token")
    assert _run(ops._ops_actor(_req("POST", m))) == (False, None)
    assert _run(ops._ops_actor(_req("POST", m), mutating=False)) == (True, "metrics-token")
    assert _run(ops._ops_admin_step_up(_req("POST", m), "release_promote")) == (False, None)


def test_deploy_token_authorises_mutating_paths(ops_env):
    ops = ops_env
    for hdr in ({"X-Ops-Deploy-Token": DT}, {"Authorization": f"Bearer {DT}"}):
        assert _run(ops._ops_actor(_req("POST", hdr))) == (True, "ops-deploy-token")
        assert _run(ops._ops_actor(_req("GET", hdr))) == (True, "ops-deploy-token")
        assert _run(ops._ops_admin_step_up(_req("POST", hdr), "release_rollback")) == \
            (True, "ops-deploy-token")
    assert _run(ops._ops_actor(_req("POST", {"X-Ops-Deploy-Token": "wrong"}))) == (False, None)


def test_transitional_flag_and_key_separation(ops_env, monkeypatch):
    ops = ops_env
    monkeypatch.setenv("OPS_ALLOW_METRICS_TOKEN_FOR_DEPLOY", "true")
    assert _run(ops._ops_actor(_req("POST", {"X-Metrics-Token": MT}))) == (True, "metrics-token")
    monkeypatch.delenv("OPS_ALLOW_METRICS_TOKEN_FOR_DEPLOY")
    # a deploy token equal to the scraper token is refused outright
    monkeypatch.setenv("OPS_DEPLOY_TOKEN", MT)
    assert _run(ops._ops_actor(_req("POST", {"X-Ops-Deploy-Token": MT}))) == (False, None)
    monkeypatch.delenv("OPS_DEPLOY_TOKEN")
    assert _run(ops._ops_actor(_req("POST", {"X-Ops-Deploy-Token": DT}))) == (False, None)


def test_ops_admin_requires_mfa(ops_env, monkeypatch):
    ops = ops_env
    import auth
    user = {"id": "u1", "role": "admin", "email": "a@x", "two_factor_enabled": False}
    monkeypatch.setattr(auth, "get_current_user", AsyncMock(return_value=user))
    with pytest.raises(HTTPException) as e:
        _run(ops._ops_actor(_req("GET")))
    assert e.value.status_code == 403 and e.value.detail["code"] == "admin_mfa_required"
    user["two_factor_enabled"] = True
    assert _run(ops._ops_actor(_req("GET"))) == (True, "a@x")
    monkeypatch.setattr(auth, "get_current_user",
                        AsyncMock(return_value={"id": "u2", "role": "user"}))
    assert _run(ops._ops_actor(_req("GET"))) == (False, None)


def test_install_and_compose_provision_ops_deploy_token():
    lib = open(os.path.join(ROOT, "deploy", "lib.sh")).read()
    assert "for f in order_auth_secret ledger_anchor_key ops_deploy_token; do" in lib
    assert "ops_deploy_token()" in lib
    assert "gen > secrets/ops_deploy_token" in open(os.path.join(ROOT, "deploy", "install.sh")).read()
    import yaml
    d = yaml.safe_load(open(os.path.join(ROOT, "docker-compose.yml")))
    assert d["services"]["backend"]["environment"]["OPS_DEPLOY_TOKEN_FILE"] == "/run/secrets/ops_deploy_token"
    assert "ops_deploy_token" in d["secrets"]


# ── 2 · real admin checks (bugs / infra) ─────────────────────────────────────
def test_bugs_admin_check_has_no_email_backdoor(monkeypatch):
    monkeypatch.setenv("ADMIN_MFA_ENFORCED", "true")
    import routes.bugs_routes as bugs
    src = open(bugs.__file__).read()
    assert "admin@trading.bot" not in src
    for u in ({"role": "user", "email": "admin@trading.bot"},
              {"role": "admin", "two_factor_enabled": False}):
        with pytest.raises(HTTPException) as e:
            bugs._require_admin(u)
        assert e.value.status_code == 403
    bugs._require_admin({"role": "admin", "two_factor_enabled": True})


def test_infra_verified_admin(monkeypatch):
    monkeypatch.setenv("ADMIN_MFA_ENFORCED", "true")
    import routes.infra_routes as infra
    assert infra._is_verified_admin({"role": "admin", "two_factor_enabled": True})
    assert not infra._is_verified_admin({"role": "admin", "two_factor_enabled": False})
    assert not infra._is_verified_admin({"role": "user", "two_factor_enabled": True})
    assert 'user.get("role") != "admin"' not in open(infra.__file__).read()


def _infra_db(owner):
    db = MagicMock()
    db.vps_agents.find_one = AsyncMock(return_value={"user_id": owner})
    db.vps_agents.update_one = AsyncMock(return_value=MagicMock(matched_count=1))
    return db


@pytest.mark.parametrize("owner,expect_step_up", [("someone-else", True), ("admin1", False)])
def test_rotate_other_users_agent_requires_step_up(monkeypatch, owner, expect_step_up):
    monkeypatch.setenv("ADMIN_MFA_ENFORCED", "true")
    import entitlements
    import step_up
    import vps_agent
    import routes.infra_routes as infra
    monkeypatch.setattr(entitlements, "enforce_feature", AsyncMock())
    monkeypatch.setattr(vps_agent, "encrypt_command_key", lambda k: "enc")
    monkeypatch.setattr(vps_agent, "hash_agent_token", lambda t: "h")
    db = _infra_db(owner)
    monkeypatch.setattr(infra, "get_db", lambda: db)
    calls = []

    async def fake_step_up(_db, user, request, action):
        calls.append(action)
        raise HTTPException(status_code=403, detail={"code": "step_up_required"})
    monkeypatch.setattr(step_up, "require_step_up", fake_step_up)
    monkeypatch.setattr(step_up, "audit_event", AsyncMock())
    admin = {"id": "admin1", "role": "admin", "two_factor_enabled": True}
    if expect_step_up:
        with pytest.raises(HTTPException) as e:
            _run(infra.rotate_agent_credentials("agt1", _req("POST"), user=admin))
        assert e.value.status_code == 403 and calls == ["agent_config_push"]
        db.vps_agents.update_one.assert_not_awaited()
    else:
        out = _run(infra.rotate_agent_credentials("agt1", _req("POST"), user=admin))
        assert out["agent_id"] == "agt1" and calls == []
    assert "agent_config_push" in step_up.STEP_UP_ACTIONS


# ── 4 · scalp retrain ────────────────────────────────────────────────────────
def test_retrain_route_requires_admin_and_rate_limits(monkeypatch):
    monkeypatch.setenv("ADMIN_MFA_ENFORCED", "true")
    import security
    import scalp.model as sm
    import routes.scalp_routes as sr
    monkeypatch.setattr(sm, "retrain", AsyncMock(return_value={"model_key": "k"}))
    monkeypatch.setattr(sr, "get_db", lambda: MagicMock())
    with pytest.raises(HTTPException) as e:
        _run(sr.retrain(_req("POST"), user={"id": "u", "role": "user"}))
    assert e.value.status_code == 403
    sm.retrain.assert_not_awaited()
    monkeypatch.setattr(security, "rate_limit",
                        AsyncMock(side_effect=HTTPException(status_code=429, detail="slow")))
    admin = {"id": "a", "role": "admin", "two_factor_enabled": True}
    with pytest.raises(HTTPException) as e:
        _run(sr.retrain(_req("POST"), user=admin))
    assert e.value.status_code == 429
    sm.retrain.assert_not_awaited()
    monkeypatch.setattr(security, "rate_limit", AsyncMock())
    assert _run(sr.retrain(_req("POST"), user=admin)) == {"model_key": "k"}


def test_retrain_single_flight_per_key(monkeypatch):
    import scalp.model as sm
    calls = []

    async def slow_once(db, symbol, broker, account_type):
        calls.append((symbol, broker))
        await asyncio.sleep(0.05)
        return {"trained": False, "model_key": sm.make_key(broker, account_type, symbol)}
    monkeypatch.setattr(sm, "_retrain_once", slow_once)

    async def run():
        a, b, c = await asyncio.gather(sm.retrain(None, "EURUSD", "X", "raw"),
                                       sm.retrain(None, "EURUSD", "X", "raw"),
                                       sm.retrain(None, "GBPUSD", "X", "raw"))
        assert not sm.retrain_in_progress("EURUSD", "X", "raw")
        return a, b, c
    a, b, c = _run(run())
    assert len(calls) == 2                       # EURUSD once, GBPUSD once
    assert a["model_key"] == b["model_key"] and (a.get("coalesced") or b.get("coalesced"))
    assert not c.get("coalesced")


def test_retrain_cpu_work_runs_off_the_event_loop(monkeypatch):
    import scalp.model as sm
    seen = {}

    def fake_train(docs, key, symbol, broker, account_type):
        seen["thread"] = threading.current_thread()
        return {"trained": False, "model_key": key, "n": len(docs)}, None
    monkeypatch.setattr(sm, "_train_from_docs", fake_train)
    cur = MagicMock()
    cur.sort.return_value = cur
    cur.to_list = AsyncMock(return_value=[{}, {}])
    db = MagicMock()
    db.scalp_decisions.find.return_value = cur
    out = _run(sm.retrain(db, "EURUSD"))
    assert out["n"] == 2 and seen["thread"] is not threading.main_thread()


# ── 3b · supervised task registry ────────────────────────────────────────────
def test_registry_restarts_crashed_task_with_backoff():
    from workers.registry import TaskRegistry
    runs = {"n": 0}

    async def flaky():
        runs["n"] += 1
        if runs["n"] < 3:
            raise RuntimeError("boom")
        await asyncio.sleep(10)

    async def run():
        reg = TaskRegistry(base_backoff=0.001, max_backoff=0.01)
        reg.spawn("flaky", flaky)
        for _ in range(200):
            if runs["n"] >= 3:
                break
            await asyncio.sleep(0.005)
        st = reg.stats()["flaky"]
        stuck = await reg.shutdown(timeout=1)
        return st, stuck, reg.get("flaky")
    st, stuck, task = _run(run())
    assert runs["n"] == 3 and st["restart_count"] == 2 and st["running"]
    assert "boom" in st["last_error"] and stuck == [] and task.cancelled()
    assert TaskRegistry(base_backoff=1, max_backoff=60).backoff_for(10) == 60


def test_registry_shutdown_cancels_everything():
    from workers.registry import TaskRegistry, named_loop
    cancelled = []

    async def forever(tag):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(tag)
            raise

    async def run():
        reg = TaskRegistry()
        for t in ("a", "b", "c"):
            reg.spawn(f"t{t}", named_loop(forever, t))
        with pytest.raises(ValueError):
            reg.spawn("ta", named_loop(forever, "dup"))
        await asyncio.sleep(0)
        return await reg.shutdown(timeout=1), reg
    stuck, reg = _run(run())
    assert stuck == [] and sorted(cancelled) == ["a", "b", "c"]
    assert not any(v["running"] for v in reg.stats().values())
    assert named_loop(forever, "x").__name__ == "forever"


def test_server_has_no_fire_and_forget_tasks_and_cancels_on_shutdown():
    src = open(os.path.join(BACKEND, "server.py")).read()
    assert "asyncio.create_task(" not in src
    tree = ast.parse(src)
    shut = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "on_shutdown")
    assert "_bg_tasks.shutdown" in ast.get_source_segment(src, shut)
    assert "start_watchdog(_bg_tasks)" in src


# ── 3a · in-process mode runs under the dedicated workers' leases ────────────
def _worker_loops(mod):
    src = open(os.path.join(BACKEND, "workers", f"{mod}.py")).read()
    call = next(n for n in ast.walk(ast.parse(src))
                if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "main")
    return call.args[0].value, [ast.unparse(e) for e in call.args[1].elts]


def test_inprocess_groups_mirror_dedicated_workers():
    from routes.ops_routes import EXPECTED_WORKERS
    src = open(os.path.join(BACKEND, "server.py")).read()
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == "_inprocess_worker_groups")
    ret = next(n for n in ast.walk(fn) if isinstance(n, ast.Return)).value
    groups = {ast.unparse(k).strip("'\""): [ast.unparse(e) for e in v.elts]
              for k, v in zip(ret.keys, ret.values)}
    for mod in EXPECTED_WORKERS:
        name, loops = _worker_loops(mod)
        assert groups[name] == loops, mod
    assert "INPROCESS_OPS_LEASE" in groups
    seg = ast.get_source_segment(src, next(
        n for n in ast.parse(src).body
        if isinstance(n, ast.FunctionDef) and n.name == "_start_inprocess_workers"))
    assert "run_worker_forever" in seg and "registry.spawn" in seg


def test_run_worker_forever_returns_to_standby_without_setting_role(monkeypatch):
    import workers.base as base
    seen = []

    async def fake_run_worker(name, loops, *, set_role=True):
        seen.append((name, set_role))
        if len(seen) >= 3:
            raise asyncio.CancelledError
    monkeypatch.setattr(base, "run_worker", fake_run_worker)
    monkeypatch.setattr(base, "LEASE_RENEW_SEC", 0)
    monkeypatch.delenv("STOIC_PROCESS_ROLE", raising=False)
    with pytest.raises(asyncio.CancelledError):
        _run(base.run_worker_forever("trading", []))
    assert seen == [("trading", False)] * 3
    assert "STOIC_PROCESS_ROLE" not in os.environ


def test_run_worker_standby_never_starts_loops_without_the_lease(monkeypatch):
    import workers.base as base
    started = []

    async def loop_fn():
        started.append(1)
        await asyncio.sleep(3600)
    monkeypatch.setattr(base, "get_db", lambda: MagicMock())
    monkeypatch.setattr(base, "_try_acquire", AsyncMock(return_value=False))
    monkeypatch.setattr(base, "LEASE_RENEW_SEC", 0.01)

    async def run():
        t = asyncio.ensure_future(base.run_worker("trading", [loop_fn], set_role=False))
        await asyncio.sleep(0.1)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
    _run(run())
    assert started == []


# ── 3c · event-loop lag metric ───────────────────────────────────────────────
def test_loop_lag_monitor_records_and_warns(monkeypatch, caplog):
    import runtime_watchdog as rw
    monkeypatch.setattr(rw, "_lag_window", rw.deque(maxlen=300))
    monkeypatch.setattr(rw, "_lag", {"last_ms": 0.0, "max_ms": 0.0, "over_threshold_total": 0,
                                     "samples_total": 0, "last_over_at": None})
    clock = {"t": 0.0}
    drifts = iter([0.010, 0.800, 0.020])

    async def fake_sleep(sec):
        try:
            clock["t"] += sec + next(drifts)
        except StopIteration:
            raise asyncio.CancelledError

    with caplog.at_level("WARNING", logger="watchdog"):
        with pytest.raises(asyncio.CancelledError):
            _run(rw.loop_lag_monitor(1.0, sleep=fake_sleep, clock=lambda: clock["t"]))
    st = rw.lag_stats()
    assert st["samples_total"] == 3 and st["over_threshold_total"] == 1
    assert 790 <= st["max_ms"] <= 810 and 15 <= st["last_ms"] <= 25
    assert any("event loop lag" in r.message for r in caplog.records)
    prom = "\n".join(rw.prometheus_lines())
    assert "stoic_event_loop_lag_ms{" in prom and "stoic_event_loop_lag_p99_ms{" in prom


def test_watchdog_start_registers_lag_monitor():
    import runtime_watchdog as rw
    from workers.registry import TaskRegistry

    async def run():
        reg = TaskRegistry()
        rw._thread_started = True          # don't spawn the sentinel thread in tests
        orig = rw._watchdog_task

        async def idle():
            await asyncio.sleep(3600)
        rw._watchdog_task = idle
        try:
            rw.start_watchdog(reg)
            names = reg.names()
        finally:
            rw._watchdog_task = orig
            await reg.shutdown(timeout=1)
        return names
    assert _run(run()) == ["event_loop_lag", "runtime_watchdog"]


# ── 1 · nginx real client IP ─────────────────────────────────────────────────
def test_nginx_real_ip_chain():
    conf = open(os.path.join(ROOT, "deploy", "nginx.conf")).read()
    for needle in ("set_real_ip_from 172.16.0.0/12;", "real_ip_header X-Forwarded-For;",
                   "real_ip_recursive on;",
                   "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
                   "proxy_set_header X-Real-IP $remote_addr;",
                   "proxy_set_header CF-Connecting-IP $remote_addr;"):
        assert needle in conf, needle
    caddy = open(os.path.join(ROOT, "deploy", "Caddyfile")).read()
    assert "header_up X-Forwarded-For {client_ip}" in caddy
    cf = open(os.path.join(ROOT, "deploy", "cloudflare", "render.sh")).read()
    assert "client_ip_headers CF-Connecting-IP" in cf and "header_up X-Forwarded-For {client_ip}" in cf
