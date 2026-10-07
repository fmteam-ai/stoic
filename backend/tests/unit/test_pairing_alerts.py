"""Pairing alerts — paired VPS terminal without a heartbeat for 10 min → ops alert + security Telegram,
recovery announced, wired into alerting.evaluate_ops_alerts (auto-resolve)."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pairing_alerts as pa  # noqa: E402

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


def _iso(s):
    return (NOW - timedelta(seconds=s)).isoformat()


def _acc(_id="a1", **kw):
    return {"_id": _id, "label": f"Demo {_id}", "broker": "ICM", "account_number": 123, **kw}


def test_silent_pairing_rules():
    r = pa.silent_pairing(_acc(installer_paired_at=_iso(900), installer_paired_hostname="VPS-1"), NOW, alert_after=600, ceiling=86400)
    assert r and r["never"] and r["silent_s"] == 900 and r["host"] == "VPS-1"
    assert pa.silent_pairing(_acc(installer_paired_at=_iso(300)), NOW, alert_after=600, ceiling=86400) is None          # within grace
    assert pa.silent_pairing(_acc(installer_paired_at=_iso(900), last_heartbeat=_iso(10)), NOW, alert_after=600, ceiling=86400) is None   # heartbeat after pairing
    r = pa.silent_pairing(_acc(installer_paired_at=_iso(900), last_heartbeat=_iso(5000)), NOW, alert_after=600, ceiling=86400)
    assert r and not r["never"]                                                                                           # old heartbeat predates the pairing
    assert pa.silent_pairing(_acc(installer_paired_at=_iso(10 * 86400)), NOW, alert_after=600, ceiling=7 * 86400) is None  # abandoned, not an incident
    assert pa.silent_pairing(_acc(installer_paired_at=_iso(900), status="deleted"), NOW, alert_after=600, ceiling=86400) is None
    assert pa.silent_pairing(_acc(), NOW, alert_after=600, ceiling=86400) is None                                         # never paired


def test_plan_raises_recovers_and_texts_carry_the_fix():
    accounts = [_acc("a1", installer_paired_at=_iso(900), installer_paired_hostname="VPS-1", installer_version="1.4"),
                _acc("a2", installer_paired_at=_iso(900), last_heartbeat=_iso(5)),
                _acc("a3")]
    p = pa.plan(accounts, {pa.dedup_key("a2"), pa.dedup_key("gone")}, NOW, url="https://stoic.example", alert_after=600, ceiling=86400)
    assert p["active"] == {pa.dedup_key("a1")}
    assert [k for k, *_ in p["raise"]] == [pa.dedup_key("a1")]
    key, text, meta, acc = p["raise"][0]
    assert text.startswith("STOIC · VPS PAIRING SILENT") and "Demo a1 (ICM · 123)" in text and "VPS-1" in text
    assert "15 min" in text and "never heartbeated" in text and "allow WebRequest for https://stoic.example" in text and "installer v1.4" in text
    assert meta["webrequest_url"] == "https://stoic.example" and meta["never_heartbeated"] is True
    assert "token" not in text.lower()                                                       # nothing secret in the chat
    long_num = pa.alert_text(_acc("a9", account_number=5012345, installer_paired_at=_iso(900)),
                             pa.silent_pairing(_acc(installer_paired_at=_iso(900)), NOW, alert_after=600, ceiling=86400), "")
    assert "…345" in long_num and "5012345" not in long_num                                  # SA6-P3: login number masked
    # a2 heartbeated after pairing → its open alert recovers; an alert whose account is gone recovers SILENTLY (N106-4)
    assert [k for k, _ in p["recovered"]] == [pa.dedup_key("a2")]
    assert "Demo a2" in dict(p["recovered"])[pa.dedup_key("a2")] and "RECOVERED" in dict(p["recovered"])[pa.dedup_key("a2")]


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    def limit(self, _n):
        return self

    def sort(self, *_a, **_k):
        return self

    def __aiter__(self):
        async def gen():
            for r in self.rows:
                yield r
        return gen()


class _Coll:
    def __init__(self, rows):
        self.rows = rows

    def find(self, *_a, **_k):
        return _Cursor(self.rows)


class _DB:
    def __init__(self, accounts, open_alerts):
        self.accounts = _Coll(accounts)
        self.ops_alerts = _Coll(open_alerts)


def test_evaluate_pushes_telegram_for_new_alerts_and_recoveries_only(monkeypatch):
    monkeypatch.setenv("PUBLIC_BACKEND_URL", "https://stoic.example")
    monkeypatch.delenv("PAIRING_HEARTBEAT_ALERT_SEC", raising=False)
    sent, raised = [], []

    async def notify(text):
        sent.append(text)
        return True

    async def raise_alert(db, kind, severity, message, dedup_key=None, meta=None, synthetic=False):
        raised.append((kind, severity, dedup_key, synthetic, message))
        return None if dedup_key == pa.dedup_key("dup") else "new-id"        # "dup" already open → raise_alert dedups

    db = _DB([_acc("a1", installer_paired_at=_iso(900), installer_paired_hostname="VPS-1"),
              _acc("dup", installer_paired_at=_iso(1200)),
              _acc("ok", installer_paired_at=_iso(900), last_heartbeat=_iso(3))],
             [{"dedup_key": pa.dedup_key("dup")}, {"dedup_key": pa.dedup_key("ok")}])
    active, n = asyncio.new_event_loop().run_until_complete(pa.evaluate(db, NOW, raise_alert=raise_alert, notify=notify))
    assert active == {pa.dedup_key("a1"), pa.dedup_key("dup")} and n == 1
    assert {r[2] for r in raised} == active and all(r[0] == pa.KIND and r[1] == "critical" for r in raised)
    assert len(sent) == 2
    assert sent[0].startswith("STOIC · VPS PAIRING SILENT") and "Demo a1" in sent[0] and "https://stoic.example" in sent[0]
    assert sent[1].startswith("STOIC · VPS PAIRING RECOVERED") and "Demo ok" in sent[1]        # dedup'd "dup" is NOT re-sent


def test_wired_into_the_ops_alert_evaluator_with_auto_resolve():
    import alerting
    assert pa.KIND in alerting.EVALUATOR_KINDS                      # auto-resolve closes it when the heartbeat lands
    src = open(os.path.join(ROOT, "backend", "alerting.py"), encoding="utf-8").read()
    body = src[src.index("async def evaluate_ops_alerts"):src.index("# 6 · AUTO-RESOLVE")]
    assert "pairing_alerts.evaluate(db, now, raise_alert=raise_alert)" in body and "active |= _pa_active" in body
    assert pa.alert_sec() == 600 and pa.max_age_sec() == 7 * 86400


def test_truncated_scan_keeps_unscanned_open_alerts_active(monkeypatch):
    monkeypatch.setattr(pa, "SCAN_LIMIT", 1)
    sent = []

    async def notify(text):
        sent.append(text)

    async def raise_alert(*a, **k):
        return None

    db = _DB([_acc("seen", installer_paired_at=_iso(900), last_heartbeat=_iso(3))],      # the one row the scan returned
             [{"dedup_key": pa.dedup_key("seen")}, {"dedup_key": pa.dedup_key("unseen")}])
    active, n = asyncio.new_event_loop().run_until_complete(pa.evaluate(db, NOW, raise_alert=raise_alert, notify=notify))
    assert pa.dedup_key("unseen") in active and pa.dedup_key("seen") not in active and n == 0   # unseen stays open, seen recovers
    assert len(sent) == 1 and "Demo seen" in sent[0]
    src = open(os.path.join(ROOT, "backend", "pairing_alerts.py"), encoding="utf-8").read()
    assert '.sort("installer_paired_at", -1).limit(SCAN_LIMIT)' in src
