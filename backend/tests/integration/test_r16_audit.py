"""Audit r16 — P1-02 statement coverage window, P1-06 public status wording,
P2-05 deploy-watch notification outbox, P2-06 Telegram MarkdownV2 bodies."""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.critical_controls]

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), ".env"))


def _run(coro):
    from conftest import run_async
    return run_async(coro)


NOW = datetime(2026, 6, 20, 12, tzinfo=timezone.utc)


def _row(days_from, days_to, sid, status="RECONCILED"):
    return {"status": status, "statement_id": sid,
            "period_from": (NOW - timedelta(days=days_from)).isoformat(),
            "period_to": (NOW - timedelta(days=days_to)).isoformat()}


def test_single_statement_covering_only_today_does_not_pass():
    """The auditor's case: one correctly signed statement for today satisfied the
    old inter-row gap loop. Now the merged coverage must start at the window."""
    import broker_statement_ledger as bl
    cov = bl.coverage_report([_row(1, 0, "today")], NOW)
    assert "STATEMENT_COVERAGE_START" in cov["reasons"] and cov["coverage_pct"] < 5


def test_contiguous_statements_cover_the_window():
    import broker_statement_ledger as bl
    rows = [_row(45, 21, "a"), _row(21, 7, "b"), _row(7, 1, "c")]
    cov = bl.coverage_report(rows, NOW)
    assert cov["reasons"] == [] and cov["coverage_pct"] > 96
    assert len(cov["intervals"]) == 1 and cov["intervals"][0]["statements"] == ["a", "b", "c"]
    assert cov["window_start"] == (NOW - timedelta(days=bl.COVERAGE_WINDOW_DAYS)).isoformat()
    assert len(cov["intervals_hash"]) == 16 and cov["formula"] == bl.COVERAGE_FORMULA_VERSION
    # identical input → identical hash (bound into the attestation)
    assert bl.coverage_report(rows, NOW)["intervals_hash"] == cov["intervals_hash"]


def test_gap_start_and_stale_are_distinct_reasons():
    import broker_statement_ledger as bl
    assert "STATEMENT_COVERAGE_GAP" in bl.coverage_report([_row(45, 21, "a"), _row(15, 1, "c")], NOW)["reasons"]
    assert "STATEMENT_COVERAGE_STALE" in bl.coverage_report([_row(45, 12, "a")], NOW)["reasons"]
    assert "STATEMENT_COVERAGE_START" in bl.coverage_report([_row(25, 1, "late")], NOW)["reasons"]
    # non-RECONCILED rows never count as coverage
    assert "STATEMENT_COVERAGE_START" in bl.coverage_report([_row(45, 1, "x", status="DISCREPANCY")], NOW)["reasons"]
    # 24 h join tolerance
    assert bl.coverage_report([_row(45, 21, "a"), _row(20.5, 1, "b")], NOW)["reasons"] == []


def test_ledger_snapshot_binds_coverage_identity_currency_and_formula():
    import broker_statement_ledger as bl
    from database import get_db
    from bson import ObjectId
    db = get_db()
    uid = str(ObjectId())
    acc = _run(db.accounts.insert_one({"user_id": uid, "trading_enabled": True, "status": "active",
                                       "account_number": "777001", "base_currency": "USD"})).inserted_id
    try:
        now = datetime.now(timezone.utc)
        _run(db.reconciliation_ledger.insert_one({
            "user_id": uid, "account_id": str(acc), "status": "RECONCILED", "statement_id": "S1", "currency": "USD",
            "key_id": "broker-k1", "statement_raw": {"issuer": "Broker"}, "ledger_seq": 1,
            "period_from": (now - timedelta(days=31)).isoformat(), "period_to": (now - timedelta(days=1)).isoformat()}))
        snap = _run(bl.ledger_snapshot(db, uid))
        a = snap["accounts"][0]
        assert a["broker_login"] == "777001" and a["currency"] == "USD" and a["issuer"] == "Broker"
        assert a["coverage_ok"] is True and a["coverage_pct"] > 96 and a["formula"] == bl.COVERAGE_FORMULA_VERSION
        assert snap["coverage_formula"] and snap["return_formula"] == bl.RETURN_FORMULA_VERSION
        assert "intervals_hash" in a and "window_start" in a and "as_of" in a
    finally:
        _run(db.accounts.delete_many({"user_id": uid}))
        _run(db.reconciliation_ledger.delete_many({"user_id": uid}))


def test_public_status_never_says_operational_while_trading_degraded():
    src = open(os.path.join(os.path.dirname(__file__), "..", "..", "routes", "portal_routes.py")).read()
    block = src[src.index("r16 P1-06"):src.index("headline = ")]
    assert 'components["ea_bridge"]["status"] != "operational"' in block and "not trading_ready" in block
    assert 'overall = "degraded"' in block
    page = open(os.path.join(os.path.dirname(__file__), "..", "..", "..", "frontend", "src", "pages", "StatusPage.jsx")).read()
    assert "All systems operational" not in page and "trading degraded" in page


def test_deploy_watch_terminal_notification_is_sent_once(monkeypatch):
    import deploy_watch as dw
    import email_sender
    from database import get_db
    db = get_db()
    sent = []

    async def fake_send(recipient, subject, html, text=None, sender=None, idempotency_key=None):
        sent.append(subject)
        return {"ok": True, "id": f"msg-{len(sent)}"}
    monkeypatch.setattr(email_sender, "send_email", fake_send)
    wid = "watch-outbox-test"
    _run(db.deploy_watch.delete_many({"_id": wid}))
    _run(db.deploy_watch_outbox.delete_many({"watch_id": wid}))
    doc = {"_id": wid, "target_url": "https://www.stoicaibot.com", "expected_sha": "abc1234",
           "notify_email": "ops@example.com", "armed_by": "ops@example.com", "armed_at": datetime.now(timezone.utc),
           "baseline_sha": None}
    _run(db.deploy_watch.insert_one(doc))
    obs = {"http": 200, "build_sha": "abc1234def", "app_env": "production"}
    r1 = _run(dw._notify(db, doc, "live", obs))
    r2 = _run(dw._notify(db, doc, "live", obs))      # retry after a crash-before-ack: deduped
    assert r1["ok"] and r1["provider_id"] == "msg-1"
    assert r2["deduped"] is True and r2["provider_id"] == "msg-1"
    assert sent == [r1 and sent[0]] and len(sent) == 1
    row = _run(db.deploy_watch_outbox.find_one({"_id": f"{wid}:live"}))
    assert row["state"] == "sent" and row["provider_id"] == "msg-1"
    assert _run(db.deploy_watch.find_one({"_id": wid}))["notified"]["provider_id"] == "msg-1"
    _run(db.deploy_watch.delete_many({"_id": wid}))
    _run(db.deploy_watch_outbox.delete_many({"watch_id": wid}))


def test_telegram_markdownv2_bodies_render_exactly():
    import warnings
    import routes.notification_routes as nr
    assert nr.TELEGRAM_TEST_TEXT.split("\n")[0] == r"🧪 *\[TEST\] STOIC AI Trader · Test Alert*"
    assert r"working\." in nr.TELEGRAM_TEST_TEXT and r"break\-even" in nr.TELEGRAM_TEST_TEXT
    assert nr.TELEGRAM_VERIFY_TEXT.format(code="123456") == (
        "🔐 STOIC chat verification code: *123456*\n\n" r"Enter it on the Notifications page\. Expires in 10 minutes\.")
    src = open(nr.__file__, encoding="utf-8").read()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        compile(src, nr.__file__, "exec")           # no invalid-escape SyntaxWarning
