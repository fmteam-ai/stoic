"""main98 N98-6 — EA 1.60 reports ACCOUNT_TRADE_MODE; the server trusts DEMO only when the broker says so."""
import asyncio
import os
import sys
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _src(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8", errors="replace").read()


def _demo_account(trade_mode=None, server="Broker-Demo", attested=True, authoritative=True):
    import broker_env as be
    acc = {"_id": ObjectId(), "mode": "live", "label": "Demo-1", "account_type": "demo", "server": server,
           "broker_server": server, "account_number": "123", "broker_account_id_reported": "123", "creds_version": 1,
           "last_heartbeat": datetime.now(timezone.utc).isoformat(),
           "ea_identity": {"installation_id": "inst1", "authoritative": authoritative, "broker_server": server,
                           "trade_mode": trade_mode}}
    if attested:
        acc["environment_attestation"] = {"environment": "DEMO", "approved_by": "admin@stoicaibot.com",
                                          "identity_hash": be.attestation_identity(acc),
                                          "proof": {"verifier": "ea_heartbeat"}}
    return acc


# ── EA source ────────────────────────────────────────────────────────────────
def test_ea_1_60_reports_account_trade_mode_on_every_heartbeat():
    mq5 = _src("backend", "static", "EmergentTradingBridge.mq5")
    assert '#property version   "1.60"' in mq5 and '#define EA_CLIENT_VERSION "1.60"' in mq5
    assert "AccountInfoInteger(ACCOUNT_TRADE_MODE)" in mq5 and "ACCOUNT_TRADE_MODE_DEMO" in mq5
    assert '\\"trade_mode\\":\\"%s\\"' in mq5 and "TradeModeString()" in mq5
    # version surfaces agree
    for rel in ("backend/routes/diagnostic_routes.py", "backend/routes/bot_routes.py"):
        assert 'LATEST_EA = "1.60"' in _src(*rel.split("/"))
    for rel in ("frontend/src/components/EaVersionStrip.jsx", "frontend/src/pages/Accounts.jsx"):
        assert 'LATEST_EA_VERSION = "1.60"' in _src(*rel.split("/"))
    import ea_capabilities as ec
    assert "trade_mode_v1" in ec.capabilities_for("1.60") and "trade_mode_v1" not in ec.capabilities_for("1.59")
    import demo_readiness as dr
    assert "1.59" in dr.DEMO_ACCEPTED_EA  # legacy terminals stay accepted during the demo


# ── broker_env classification ────────────────────────────────────────────────
def test_broker_real_beats_every_declared_field_and_voids_attestation():
    import broker_env as be
    acc = _demo_account(trade_mode="real")
    acc["broker_environment"] = "DEMO"
    assert be.broker_environment(acc) == "LIVE"
    assert be.attested_environment(acc) == "LIVE"
    assert be.attestation_state(acc) == "invalidated"
    proof = be.demo_proof(acc)
    assert proof["checks"]["broker_not_real_money"] is False and not proof["mandatory_ok"]
    assert not proof["override_eligible"] and proof["reported_trade_mode"] == "real"
    # contest money is real money too; an unverified chain reporting real is still fail-closed
    assert be.attested_environment(_demo_account(trade_mode="contest")) == "LIVE"
    assert be.broker_environment(_demo_account(trade_mode="real", authoritative=False)) == "LIVE"
    assert be.broker_environment({"mode": "live", "account_type": "demo", "account_trade_mode": "real"}) == "LIVE"


def test_broker_demo_replaces_server_name_heuristic_but_needs_authoritative_chain():
    import broker_env as be
    acc = _demo_account(trade_mode="demo", server="Broker-Server7")   # not demo-named
    proof = be.demo_proof(acc)
    assert proof["checks"]["server_demo_named"] is False and proof["checks"]["broker_reports_demo"] is True
    assert proof["ok"] and not proof["override_eligible"] and proof["reported_trade_mode"] == "demo"
    assert be.attested_environment(acc) == "DEMO"
    # the same word from an UNVERIFIED chain is not evidence
    weak = _demo_account(trade_mode="demo", server="Broker-Server7", authoritative=False)
    assert be.demo_proof(weak)["checks"]["broker_reports_demo"] is False and not be.demo_proof(weak)["ok"]


def test_legacy_ea_without_trade_mode_keeps_the_existing_path():
    import broker_env as be
    acc = _demo_account(trade_mode=None)
    proof = be.demo_proof(acc)
    assert proof["checks"]["broker_not_real_money"] is True and proof["checks"]["broker_reports_demo"] is False
    assert proof["ok"] and proof["reported_trade_mode"] is None          # demo-named server still suffices
    assert be.attested_environment(acc) == "DEMO"
    plain = _demo_account(trade_mode=None, server="Broker-Server7")
    assert be.demo_proof(plain)["override_eligible"]                      # audited override path intact


# ── heartbeat persistence + contradiction alert ──────────────────────────────
def test_heartbeat_stores_trade_mode_and_alerts_when_broker_contradicts_attestation():
    import routes.bridge_routes as br
    db = FakeDb()
    acc = _demo_account(trade_mode=None)
    db.accounts.rows.append(acc)
    run(br._trade_mode_contradiction(db, acc, "demo", "2026-06-01T00:00:00+00:00"))
    assert db.ops_alerts.rows == []
    run(br._trade_mode_contradiction(db, acc, "real", "2026-06-01T00:00:00+00:00"))
    assert len(db.ops_alerts.rows) == 1
    alert = db.ops_alerts.rows[0]
    assert alert["kind"] == "demo_attestation_contradicted" and alert["severity"] == "critical"
    assert alert["meta"]["was_attested"] is True and alert["meta"]["trade_mode"] == "real"
    chained = db.admin_audit_log.rows[-1]
    assert chained["action"] == "account_environment_contradicted" and chained.get("entry_hash")
    # declared-only (never attested) DEMO → warning, not critical
    db2 = FakeDb()
    run(br._trade_mode_contradiction(db2, _demo_account(trade_mode=None, attested=False), "contest", "2026-06-01T00:00:00+00:00"))
    assert db2.ops_alerts.rows[0]["severity"] == "warning" and db2.ops_alerts.rows[0]["meta"]["was_attested"] is False
    # a LIVE-declared account reporting real is business as usual — no alert
    db3 = FakeDb()
    live = {"_id": ObjectId(), "mode": "live", "account_type": "live", "server": "Broker-Live", "label": "L"}
    run(br._trade_mode_contradiction(db3, live, "real", "2026-06-01T00:00:00+00:00"))
    assert db3.ops_alerts.rows == []
    # heartbeat wiring: payload field accepted, stored top-level + on ea_identity
    from models import BridgeHeartbeat
    hb = BridgeHeartbeat(bridge_token="t", balance=1, equity=1, trade_mode="demo")
    assert hb.trade_mode == "demo"
    src = _src("backend", "routes", "bridge_routes.py")
    assert 'set_doc["account_trade_mode"] = hb_trade_mode' in src and '"trade_mode": hb_trade_mode or None' in src


# ── admin surfaces ───────────────────────────────────────────────────────────
def test_admin_env_row_and_readiness_expose_broker_trade_mode():
    from routes.admin_routes import _env_row
    row = _env_row(_demo_account(trade_mode="demo", server="Broker-Server7"))
    assert row["broker_trade_mode"] == "demo" and row["effective"] == "DEMO" and row["proof"]["ok"]
    row = _env_row(_demo_account(trade_mode="real"))
    assert row["broker_trade_mode"] == "real" and row["effective"] == "LIVE" and row["attestation_state"] == "invalidated"
    import demo_readiness as dr
    base = {"label": "D", "heartbeat_fresh": True, "ea_current": True, "attested_demo": True,
            "position_mode_explicit": True, "caps_explicit": True, "netting": False}
    by_id = lambda rows: {c["id"]: c for c in dr.fleet_checks(rows)}  # noqa: E731
    assert by_id([dict(base, broker_trade_mode="demo")])["fleet_broker_demo"]["status"] == "pass"
    assert by_id([dict(base, broker_trade_mode=None)])["fleet_broker_demo"]["status"] == "warn"
    c = by_id([dict(base, broker_trade_mode="demo"), dict(base, label="X", broker_trade_mode="real")])["fleet_broker_demo"]
    assert c["status"] == "fail" and "X" in c["detail"]
    jsx = _src("frontend", "src", "components", "admin", "AccountEnvironmentsPanel.jsx")
    assert "account-env-broker-mode-" in jsx and "BROKER SAYS DEMO" in jsx and "broker_not_real_money" in jsx
