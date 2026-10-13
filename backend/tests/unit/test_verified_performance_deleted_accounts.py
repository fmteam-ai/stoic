"""Verified Performance — deals of hard-deleted accounts are the audit trail, not the headline.

Regression for the production report: broker_deals of a hard-deleted account (72 trades, +$3.4k) appeared in the
headline as "ACCOUNT-1 · — · UNKNOWN" while the integrity header counted db.trades ("100% of 1"). Now the headline,
equity curve and integrity stamp come from the SAME deal set (current, non-deleted accounts); deleted accounts are
reported under `archived`, labelled from the tombstone the delete path writes (environment = server classification at
deletion time); the attestation freshness check ignores archived deals; nothing is deleted."""
import asyncio
import os
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _deal(uid, aid, ts, profit, entry="out"):
    return {"user_id": uid, "account_id": aid, "deal_time": ts, "profit": profit, "commission": -0.5, "swap": 0,
            "deal_entry": entry}


def _seed(db, uid):
    now = int(datetime.now(timezone.utc).timestamp())
    live = ObjectId()
    db.accounts.rows.append({"_id": live, "user_id": uid, "label": "IC Markets live", "broker": "IC Markets", "mode": "live",
                             "status": "active", "account_type": "live", "broker_environment": "LIVE",
                             "last_heartbeat": datetime.now(timezone.utc).isoformat(),
                             "last_reconciled_at": datetime.now(timezone.utc).isoformat(), "reconciliation_seq": 3})
    deleted = "6abe73a2aaaaaaaaaaaa11d14d"                                   # hard-deleted RoboForex demo (prod shape)
    db.broker_deals.rows += [_deal(uid, str(live), now - 7200, 10.0), _deal(uid, str(live), now - 3600, -4.0),
                             _deal(uid, str(live), now - 1800, 0.0, entry="in")]
    db.broker_deals.rows += [_deal(uid, deleted, now - 86400 * 3 + i, 50.0 if i % 3 else -5.0) for i in range(72)]
    db.trades.rows += [{"user_id": uid, "account_id": str(live), "status": "closed", "origin": "auto", "broker_deal_id": "d1"},
                       {"user_id": uid, "account_id": str(live), "status": "closed", "origin": "auto", "broker_deal_id": None},
                       {"user_id": uid, "account_id": deleted, "status": "closed", "origin": "auto", "broker_deal_id": None,
                        "pnl_estimated": True}]
    return live, deleted


def test_headline_counts_only_current_accounts_and_integrity_uses_the_same_deal_set():
    import routes.performance_routes as pr
    db = FakeDb()
    uid = str(ObjectId())
    live, deleted = _seed(db, uid)
    db.account_tombstones.rows.append({"_id": deleted, "user_id": uid, "label": "RoboForex demo", "broker": "RoboForex",
                                       "environment": "DEMO", "mode": "live", "deleted_at": "2026-10-01T10:00:00+00:00"})
    with patch("broker_statement_ledger.ledger_gate", AsyncMock(return_value=[])):
        out = _run(pr._verified_payload(db, uid))
    # headline: the two closed live deals only (10 - 0.5) + (-4 - 0.5) + (0 - 0.5)
    assert out["overall"]["deals"] == 3 and out["overall"]["closed_positions"] == 2 and out["overall"]["net_pnl"] == 4.5
    assert [a["label"] for a in out["accounts"]] == ["IC Markets live"] and out["accounts"][0]["environment"] == "LIVE"
    assert all(abs(pt["net"]) < 100 for pt in out["equity_curve"])            # the +$3.4k never enters the curve
    # archived: the deleted account, labelled from the tombstone — not "UNKNOWN", not ACCOUNT-n
    assert len(out["archived"]) == 1
    arch = out["archived"][0]
    assert arch["label"] == "RoboForex demo" and arch["broker"] == "RoboForex" and arch["environment"] == "DEMO"
    assert arch["deals"] == 72 and arch["tombstone"] is True and arch["deleted_at"].startswith("2026-10-01")
    assert arch["account_id_suffix"] == "11d14d"
    # integrity header agrees with the headline: 2 broker-verified closed positions + 1 unmatched bot trade on the live account
    integ = out["integrity"]
    assert integ["broker_verified_trades"] == 2 and integ["closed_trades"] == 3 and integ["verified_pct"] == 66.7
    assert integ["estimated_or_unknown_excluded"] == 0                         # the deleted account's estimate is out of scope
    assert integ["accounts_included"] == 1 and integ["archived_accounts_excluded"] == 1 and integ["archived_deals_excluded"] == 72
    # nothing was deleted
    assert len(db.broker_deals.rows) == 75


def test_masked_public_payload_masks_archived_labels_and_unknown_only_without_tombstone():
    import routes.performance_routes as pr
    db = FakeDb()
    uid = str(ObjectId())
    _seed(db, uid)                                                             # no tombstone written
    with patch("broker_statement_ledger.ledger_gate", AsyncMock(return_value=[])):
        out = _run(pr._verified_payload(db, uid, mask=True))
        plain = _run(pr._verified_payload(db, uid))
    assert out["accounts"][0]["label"] == "ACCOUNT-1"
    assert out["archived"][0]["label"] == "ARCHIVED-1" and out["archived"][0]["environment"] == "UNKNOWN"
    assert out["archived"][0]["tombstone"] is False
    assert plain["archived"][0]["label"] == "DELETED ACCOUNT …11d14d"       # honest when the tombstone is missing


def test_attestation_freshness_ignores_deals_of_deleted_accounts(monkeypatch):
    import routes.performance_routes as pr
    db = FakeDb()
    uid = str(ObjectId())
    live = ObjectId()
    db.accounts.rows.append({"_id": live, "user_id": uid, "label": "L", "mode": "live", "status": "active",
                             "trading_enabled": True, "account_type": "live", "broker_environment": "LIVE"})
    now = int(datetime.now(timezone.utc).timestamp())
    db.broker_deals.rows += [_deal(uid, str(live), now - 86400 * 9, 1.0),            # live account: 9 days stale
                             _deal(uid, "deadbeefdeadbeefdeadbeef", now - 60, 1.0)]  # deleted account: fresh — must not count
    monkeypatch.setattr("routes.trade_routes.trade_stats", AsyncMock(return_value={"reconciliation": {"status": "RECONCILED"}}))
    monkeypatch.setattr("state_contract.contract", AsyncMock(return_value={"accounts": [{"account_enabled": True, "position_truth": "FRESH"}]}))
    monkeypatch.setattr("synthetic_data.is_synthetic_account", lambda a: False)
    monkeypatch.setattr(pr, "_attestation_environment", lambda a: "LIVE")
    with patch("broker_statement_ledger.ledger_gate", AsyncMock(return_value=[])):
        reasons = _run(pr._attestation_gate(db, uid))
    assert "BROKER_DATA_STALE" in reasons


def test_tombstone_records_user_environment_and_never_expires_when_it_labels_deals():
    import account_tombstones as at
    db = FakeDb()
    oid = ObjectId()
    acc = {"_id": oid, "user_id": "u1", "label": "RoboForex demo", "broker": "RoboForex", "mode": "live",
           "account_type": "demo", "broker_environment": "DEMO", "broker_server": "RoboForex-Demo",
           "bridge_token_hash": "h1", "account_number": "123"}
    db.broker_deals.rows.append(_deal("u1", str(oid), 1, 1.0))
    tomb = _run(at.write_tombstone(db, acc, deleted_by="u1"))
    assert tomb["user_id"] == "u1" and tomb["server"] == "RoboForex-Demo" and tomb["declared_environment"] == "DEMO"
    assert tomb["environment"] == "LIVE"                                    # no attestation left → server classification is LIVE (fail closed)
    assert tomb["has_deals"] is True and tomb["expires_at"] is None          # labels deals → durable (TTL index skips None)
    assert tomb["token_hashes"] == ["h1"] and "bridge_token" not in str(tomb).replace("bridge_token_hash", "")
    # attested DEMO account → DEMO on the tombstone
    from broker_env import attestation_identity
    acc2 = {**acc, "_id": ObjectId(), "broker_trade_mode": "demo"}
    acc2["environment_attestation"] = {"environment": "DEMO", "approved_by": "admin", "identity_hash": attestation_identity(acc2),
                                       "proof": {"verifier": "ea_heartbeat"}}
    tomb2 = _run(at.write_tombstone(db, acc2, deleted_by="u1"))
    assert tomb2["environment"] == "DEMO" and tomb2["has_deals"] is False and tomb2["expires_at"] is not None


def test_backfill_script_writes_missing_tombstones_only_for_orphans_and_never_overwrites():
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import importlib
    bf = importlib.import_module("backfill_account_tombstones")
    db = FakeDb()
    live = ObjectId()
    db.accounts.rows.append({"_id": live, "user_id": "u1"})
    db.broker_deals.rows += [_deal("u1", str(live), 5, 1.0), _deal("u1", "orphan1", 6, 2.0), _deal("u1", "orphan1", 7, 2.0),
                             _deal("u1", "orphan2", 8, 3.0)]
    db.account_tombstones.rows.append({"_id": "orphan2", "label": "kept"})
    orphans = _run(bf.orphan_deal_accounts(db))
    assert set(orphans) == {"orphan1", "orphan2"} and orphans["orphan1"]["deals"] == 2
    written = _run(bf.backfill(db, account="orphan1", all_orphans=False, environment="demo", broker="RoboForex",
                               label="RoboForex demo", by="test"))
    assert written == ["orphan1"]
    row = _run(db.account_tombstones.find_one({"_id": "orphan1"}))
    assert row["environment"] == "DEMO" and row["backfilled"] is True and row["expires_at"] is None and row["user_id"] == "u1"
    assert _run(bf.backfill(db, account=str(live), all_orphans=False, environment="DEMO", broker=None, label=None, by="t")) == []
    assert _run(bf.backfill(db, account=None, all_orphans=True, environment="UNKNOWN", broker=None, label=None, by="t")) == []
    assert _run(db.account_tombstones.find_one({"_id": "orphan2"}))["label"] == "kept"
    with pytest.raises(ValueError):
        bf.tombstone_doc("x", {}, environment="real", broker=None, label=None, by="t")


def test_frontend_renders_the_archived_section_on_both_surfaces():
    comp = open(os.path.join(ROOT, "frontend", "src", "components", "VerifiedPerf.jsx"), encoding="utf-8").read()
    assert "export function ArchivedAccountsTable" in comp and 'data-testid="archived-accounts"' in comp
    assert "DELETED ACCOUNTS · NOT IN THE HEADLINE · KEPT AS AUDIT TRAIL" in comp
    assert "archived_deals_excluded" in comp                                   # integrity stamp names the exclusion
    for page in ("VerifiedPerformance.jsx", "PublicPerformance.jsx"):
        src = open(os.path.join(ROOT, "frontend", "src", "pages", page), encoding="utf-8").read()
        assert "<ArchivedAccountsTable archived={d.archived} />" in src, page
