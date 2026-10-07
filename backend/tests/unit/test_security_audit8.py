"""Security audit #8 P3 hardenings: metrics-token acks never suppress re-raises; single tolerant
_parse_ts; policy-migration signing deps pinned to backend/requirements.txt."""
import asyncio
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


class _Alerts:
    def __init__(self, acked_by, acked_at):
        self.acked_by, self.acked_at, self.inserted = acked_by, acked_at, 0

    async def find_one(self, flt, sort=None, projection=None):
        if flt.get("acked_at") is None:
            return None
        if re.match(flt["acked_by"]["$not"]["$regex"], self.acked_by):
            return None
        return {"acked_at": self.acked_at}

    async def insert_one(self, doc):
        self.inserted += 1
        return type("R", (), {"inserted_id": "x"})()

    async def update_one(self, *a, **k):
        return None


def _run(acked_by, acked_at):
    import alerting
    db = type("DB", (), {})()
    db.ops_alerts = _Alerts(acked_by, acked_at)

    async def go():
        return await alerting.raise_alert(db, "demo_account_reports_real", "critical", "m", dedup_key="demo_mode:a")
    return asyncio.new_event_loop().run_until_complete(go()), db.ops_alerts.inserted


def test_only_human_admin_acks_suppress_reraise():
    recent = datetime.now(timezone.utc) - timedelta(minutes=5)
    assert _run("admin@x", recent) == (None, 0)
    assert _run("metrics-token", recent)[1] == 1          # leaked scraper token cannot mute for 6 h
    assert _run("system:auto-resolved", recent)[1] == 1
    assert _run("admin@x", "not-a-date")[1] == 1          # junk acked_at → no crash, no suppression


def test_single_parse_ts_definition_tolerant():
    import alerting
    src = open(os.path.join(ROOT, "backend", "alerting.py")).read()
    assert src.count("def _parse_ts(") == 1
    assert alerting._parse_ts("junk") is None
    assert alerting._parse_ts("2026-01-01T00:00:00Z").tzinfo is not None
    assert alerting._parse_ts(datetime(2026, 1, 1)).tzinfo is not None


def test_policy_workflow_signing_deps_pinned_to_requirements():
    wf = open(os.path.join(ROOT, ".github", "workflows", "policy-migration.yml")).read()
    req = open(os.path.join(ROOT, "backend", "requirements.txt")).read()
    m = re.search(r"pip install -q ([^#\n]+)", wf)
    assert m
    pins = m.group(1).split()
    assert len(pins) == 3 and all("==" in p for p in pins)
    for p in pins:
        assert f"\n{p}\n" in f"\n{req}" or req.startswith(p + "\n"), p
