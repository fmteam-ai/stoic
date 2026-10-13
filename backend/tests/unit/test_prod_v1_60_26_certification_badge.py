"""prod v1.60.26 — the account certification badge (/accounts/certification) could never reach 14/14:
`account_type` expected hedging/netting but the field stores the LOT type; `clock_sync` read `broker_utc_offset_sec`
while the EA reports the offset in heartbeat `broker_time`; `history_sync` needed `last_full_sync_at`, which only a
deep sync nobody requested ever stamps (and expired every 24 h). Now the badge reads the real sources and the heartbeat
bootstraps exactly one deep sync."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _iso(ago_s=0):
    return (datetime.now(timezone.utc) - timedelta(seconds=ago_s)).isoformat()


def _full_account(**over):
    from routes.diagnostic_routes import LATEST_EA
    acc = {"_id": ObjectId(), "user_id": "u1", "label": "Live", "mode": "live", "status": "active",
           "account_type": "standard",                                   # the LOT type — never hedging/netting
           "ea_version": LATEST_EA, "bridge_token_hash": "h", "last_heartbeat": _iso(5),
           "margin_mode": "hedging", "margin_mode_reported_at": _iso(5),  # EA ACCOUNT_MARGIN_MODE
           "symbol_specs": {"XAUUSD": {"stops_level_points": 50}}, "symbol_specs_updated_at": _iso(60),
           "spreads_updated_at": _iso(10),
           "broker_time_info": {"server_gmt_offset_sec": 10799, "server_time": 1}, "broker_time_updated_at": _iso(5),
           "last_full_sync_at": _iso(3 * 86400),                          # older than 24 h — must still be OK
           "available_symbols": ["XAUUSD"], "demo_certified_at": _iso(86400)}
    acc.update(over)
    return acc


def _cert(db):
    import routes.account_routes as ar
    ar.get_db = lambda: db
    return _run(ar.accounts_certification(user={"id": "u1"}))["items"][0]


def test_badge_reaches_14_of_14_from_what_the_ea_actually_reports():
    db = FakeDb()
    _run(db.accounts.insert_one(_full_account()))
    item = _cert(db)
    by = {c["key"]: c for c in item["checks"]}
    assert item["total"] == 14 and item["passed"] == 14 and item["certified"] is True, [k for k, c in by.items() if not c["ok"]]
    assert by["account_type"]["ok"] and by["account_type"]["value"] == "hedging · ea"
    assert by["clock_sync"]["ok"] and by["clock_sync"]["value"] == "+10800s · ea_broker_time"
    assert by["history_sync"]["ok"] and by["history_sync"]["value"].startswith("72h ago")


def test_badge_still_fails_when_the_facts_are_really_missing():
    db = FakeDb()
    _run(db.accounts.insert_one(_full_account(margin_mode=None, broker_time_info=None, last_full_sync_at=None)))
    by = {c["key"]: c for c in _cert(db)["checks"]}
    assert by["account_type"]["ok"] is False and by["account_type"]["value"].startswith("unknown · ")      # registry/default is not a fact
    assert by["clock_sync"]["ok"] is False and by["clock_sync"]["value"] == "unknown"
    assert by["history_sync"]["ok"] is False and by["history_sync"]["value"] == "never"


def test_clock_sync_falls_back_to_offset_learned_from_deals_and_admin_mode_counts():
    db = FakeDb()
    _run(db.accounts.insert_one(_full_account(margin_mode=None, broker_time_info=None, broker_utc_offset_sec=7200,
                                              position_mode_override={"mode": "netting", "by": "admin@x"})))
    by = {c["key"]: c for c in _cert(db)["checks"]}
    assert by["clock_sync"]["ok"] and by["clock_sync"]["value"] == "+7200s · learned_from_deals"
    assert by["account_type"]["ok"] and by["account_type"]["value"] == "netting · admin"


def test_history_sync_reports_a_stuck_request_but_not_a_pending_fresh_one():
    db = FakeDb()
    _run(db.accounts.insert_one(_full_account(pending_history_sync={"requested_at": _iso(3600), "requested_by": "certification_bootstrap"})))
    by = {c["key"]: c for c in _cert(db)["checks"]}
    assert by["history_sync"]["ok"] is False and by["history_sync"]["value"] == "sync requested, EA has not confirmed"
    db2 = FakeDb()
    _run(db2.accounts.insert_one(_full_account(last_full_sync_at=None, pending_history_sync={"requested_at": _iso(30), "requested_by": "certification_bootstrap"})))
    by2 = {c["key"]: c for c in _cert(db2)["checks"]}
    assert by2["history_sync"]["ok"] is False and by2["history_sync"]["value"] == "pending"


def test_heartbeat_bootstraps_exactly_one_deep_sync():
    src = open(os.path.join(ROOT, "backend", "routes", "bridge_routes.py"), encoding="utf-8").read()
    block = src.split("certification `history_sync` bootstrap")[1].split("hb_update: dict")[0] if "hb_update: dict" in src.split("certification `history_sync` bootstrap")[1] else src.split("certification `history_sync` bootstrap")[1][:1200]
    assert 'not acc.get("last_full_sync_at") and not acc.get("history_sync_bootstrap_at")' in block
    assert '"pending_history_sync" not in set_doc' in block and "not mismatch" in block
    assert '"requested_by": "certification_bootstrap"' in block and 'set_doc["history_sync_bootstrap_at"] = now_iso' in block
    # the guard flag is written in the same update, so a terminal that never confirms is asked exactly once
    assert block.index('set_doc["history_sync_bootstrap_at"]') < block.index('set_doc["pending_history_sync"]')
