"""iter-139 — broker registry, immutable artifacts, ops console expansion,
host-agent telemetry, identity-enforcement conformance."""
import os
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), ".env"))


def _run(coro):
    from conftest import run_async
    return run_async(coro)


def _db():
    from database import get_db
    return get_db()


# ---------------------------------------------------------------- registry

def test_registry_seed_idempotent():
    from broker_registry import ensure_seed, SEED_BROKERS
    db = _db()

    async def _t():
        await ensure_seed(db)
        n1 = await db.broker_registry.count_documents({})
        again = await ensure_seed(db)
        assert again == 0  # second run inserts nothing
        assert n1 >= len(SEED_BROKERS)
    _run(_t())


def test_registry_resolve_and_symbol_map():
    from broker_registry import (ensure_seed, resolve_registry,
                                 registry_symbol_for, invalidate_cache)
    db = _db()

    async def _t():
        await ensure_seed(db)
        invalidate_cache()
        entry = await resolve_registry(db, "XMGlobal-MT5 5")
        assert entry and entry["broker_id"] == "xm"
        # XM maps canonical XAUUSD to broker ticker GOLD
        assert await registry_symbol_for("XMGlobal-MT5 5", "XAUUSD") == "GOLD"
        # Exness suffix variant
        assert await registry_symbol_for("Exness-MT5Real8", "XAUUSD") == "XAUUSDm"
        # identity-mapped brokers return the ticker (same as base)
        assert await registry_symbol_for("ICMarkets-Live01", "XAUUSD") == "XAUUSD"
        # unknown broker → None
        invalidate_cache()
        assert await registry_symbol_for("TotallyUnknown-Live", "XAUUSD") is None
    _run(_t())


def test_registry_admin_crud_validation():
    from routes.broker_registry_routes import create_broker, update_broker, delete_broker
    admin = {"role": "admin", "two_factor_enabled": True}
    db = _db()

    async def _t():
        payload = {"broker_id": "testbrk139", "name": "Test Broker",
                   "server_aliases": ["TestBroker-Live"],
                   "symbol_map": {"xauusd": "XAU.t"},
                   "contract_specs": {}, "stop_level_points": 5,
                   "freeze_level_points": 0}
        try:
            out = await create_broker(payload, user=admin)
            assert out["symbol_map"] == {"XAUUSD": "XAU.t"}  # canonical upper
            with pytest.raises(HTTPException):  # duplicate id
                await create_broker(payload, user=admin)
            upd = await update_broker("testbrk139",
                                      {**payload, "stop_level_points": 12},
                                      user=admin)
            assert upd["stop_level_points"] == 12
            with pytest.raises(HTTPException):  # non-admin blocked
                await create_broker(payload, user={"role": "user"})
        finally:
            await db.broker_registry.delete_many({"broker_id": "testbrk139"})
    _run(_t())


def test_execution_consults_registry():
    import inspect
    import execution
    src = inspect.getsource(execution)
    assert "registry_symbol_for" in src
    # manual override must still win (registry consulted only in fallback)
    assert src.index("user_suffix") < src.index("registry_symbol_for")


# ---------------------------------------------------------------- artifacts

def test_immutable_artifact_endpoint():
    import hashlib
    from pathlib import Path
    from server import artifact_by_hash

    mq5 = Path(__file__).parent.parent / "static" / "EmergentTradingBridge.mq5"
    digest = hashlib.sha256(mq5.read_bytes()).hexdigest()
    resp = _run(artifact_by_hash(digest))
    assert getattr(resp, "status_code", 200) == 200
    assert resp.headers.get("x-artifact-sha256") == digest
    assert "immutable" in resp.headers.get("cache-control", "")
    # wrong digest → 404, malformed → 400
    bad = _run(artifact_by_hash("0" * 64))
    assert bad.status_code == 404
    ugly = _run(artifact_by_hash("nothex"))
    assert ugly.status_code == 400


def test_manifest_pins_content_addressed_urls():
    from vps_pathb import build_artifact_manifest
    m = build_artifact_manifest()
    ea = next(a for a in m["artifacts"] if a["name"] == "stoic-ea")
    if ea["sha256"]:
        assert ea["url"] == f"/api/artifacts/{ea['sha256']}"
        assert ea.get("mutable_url") == "/api/ea-script"


# ---------------------------------------------------------------- identity

def test_identity_enforcement_conformance():
    """Operational decisions key off verified identity, never display labels."""
    import inspect
    from routes import bridge_routes
    src = inspect.getsource(bridge_routes)
    # bridge auth resolves accounts by token, never by display_name
    assert "display_name" not in inspect.getsource(bridge_routes._account_by_token)
    import execution
    esrc = inspect.getsource(execution)
    assert "display_name" not in esrc, \
        "execution path must not branch on user-defined labels"


# ---------------------------------------------------------------- ops console

def test_ops_console_new_sections():
    from routes.ops_console import ops_console
    out = _run(ops_console(user={"role": "admin", "two_factor_enabled": True}))
    assert "success_24h" in out["deployments"]
    assert "billing_feed" in out and isinstance(out["billing_feed"], list)
    sec = out["security"]
    for key in ("failed_auth_recent", "audit_chain_ok",
                "admin_mfa_enforced", "email_otp_login"):
        assert key in sec
    assert "host_agents" in out


def test_agent_heartbeat_accepts_new_telemetry():
    import inspect
    import vps_agent
    src = inspect.getsource(vps_agent)
    for key in ("disk_free_pct", "broker_latency_ms", "restarts_24h",
                "service_uptime_sec"):
        assert key in src, key


def test_runbooks_include_supply_chain():
    from runbooks_content import get_runbooks
    ids = {r["id"] for r in get_runbooks()["runbooks"]}
    assert "supply-chain" in ids
