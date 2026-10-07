"""Security-alert channel test button (Admin → Integrations) and the legacy scalp-row migration (A15-7)."""
import asyncio
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _read(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


class _State:
    def __init__(self):
        self.doc = None

    async def find_one(self, *_a, **_k):
        return self.doc

    async def update_one(self, _f, upd, upsert=False):
        self.doc = {**(self.doc or {}), **upd.get("$set", {})}


class _DB:
    def __init__(self):
        self.platform_state = _State()
        self.audit = []


# ── security alert test ──────────────────────────────────────────────────────────────────────────
def test_security_alert_test_sends_once_records_and_never_leaks_the_token():
    import security_alert_test as sat
    db = _DB()
    sent, chained = [], []

    async def fake_send(text, *, creds=None):
        sent.append((text, creds))
        return True

    async def fake_chain(_db, entry, collection="admin_audit_log"):
        chained.append(entry)
        return entry

    env = {"SECURITY_AGENT_TELEGRAM_BOT_TOKEN": "123456:SECRET-TOKEN", "SECURITY_AGENT_TELEGRAM_CHAT_ID": "-1001234567",
           "SECURITY_AGENT_TELEGRAM_BOT_TOKEN_FILE": "/run/secrets/security_telegram_token", "APP_ENV": "production"}
    with patch.dict(os.environ, env), patch("security_agent.alerts.send_telegram", fake_send), patch("audit_chain.append_chained", fake_chain):
        st = asyncio.new_event_loop().run_until_complete(sat.status(db))
        assert st["configured"] and st["token_source"] == "secrets_file" and st["chat_id_masked"] == "-1…567" and st["last_test"] is None
        res = asyncio.new_event_loop().run_until_complete(sat.send_test(db, {"id": "u1", "email": "admin@x"}))
        assert res["ok"] and "delivered" in res["detail"] and "SECRET-TOKEN" not in str(res)
        assert len(sent) == 1 and sent[0][0].startswith("STOIC · SECURITY ALERT CHANNEL TEST") and "admin@x" in sent[0][0] and "production" in sent[0][0]
        assert sent[0][1] == ("123456:SECRET-TOKEN", "-1001234567")
        assert chained and chained[0]["action"] == "security_alert_test" and "SECRET-TOKEN" not in str(chained[0]) and chained[0]["target_id"] == "-1…567"
        st = asyncio.new_event_loop().run_until_complete(sat.status(db))
        assert st["last_test"]["ok"] is True and st["last_test"]["by"] == "admin@x"
    with patch.dict(os.environ, {"SECURITY_AGENT_TELEGRAM_BOT_TOKEN": "", "SECURITY_AGENT_TELEGRAM_CHAT_ID": ""}):
        res = asyncio.new_event_loop().run_until_complete(sat.send_test(_DB(), {"id": "u1", "email": "admin@x"}))
        assert not res["ok"] and "not configured" in res["detail"]


def test_security_alert_routes_and_card_wired():
    src = _read("backend/routes/admin_routes.py")
    body = src[src.index('@router.post("/admin/security-alerts/test")'):]
    assert "require_admin(user)" in body[:600] and 'rate_limit(db, "security_alert_test", f"user:{user[\'id\']}", 3, 600' in body
    assert '@router.get("/admin/security-alerts/status")' in src
    page = _read("frontend/src/pages/AdminIntegrations.jsx")
    assert "<SecurityAlertsCard />" in page
    card = _read("frontend/src/components/admin/SecurityAlertsCard.jsx")
    for tid in ("security-alert-test-btn", "security-alert-status", "security-alert-test-result", "integration-card-security-telegram"):
        assert tid in card, tid
    assert 'api.post("/admin/security-alerts/test")' in card


# ── scalp rows migration ─────────────────────────────────────────────────────────────────────────
def test_scalp_rows_migration_rewrites_legacy_rows_into_the_scalp_counter():
    import scalp_rows_migration as srm
    from account_reservations import trade_counter_filter
    legacy = {"_id": "t1", "origin": "scalp", "status": "open", "symbol": "EURUSD"}
    row = srm.rewrite_row(legacy)
    assert row["origin"] == "auto" and row["engine"] == "scalp" and row["legacy_origin"] == "scalp" and row["origin_migrated_at"]

    def matches(doc, flt):
        for k, v in flt.items():
            if isinstance(v, dict) and "$ne" in v:
                if doc.get(k) == v["$ne"]:
                    return False
            elif doc.get(k) != v:
                return False
        return True

    assert not matches(legacy, trade_counter_filter("scalp")) and not matches(legacy, trade_counter_filter("auto"))   # was in NEITHER counter
    assert matches(row, trade_counter_filter("scalp")) and not matches(row, trade_counter_filter("auto"))             # now exactly in scalp
    assert srm.LEGACY_FILTER == {"origin": "scalp"}


def test_scalp_rows_migration_is_idempotent_and_audited():
    import scalp_rows_migration as srm

    class _Res:
        modified_count = 3

    class _Cur:
        def __init__(self, rows): self.rows = rows
        def limit(self, n): self.rows = self.rows[:n]; return self
        def __aiter__(self):
            async def g():
                for r in self.rows:
                    yield r
            return g()

    class _Trades:
        def __init__(self):
            self.rows = [{"_id": i, "origin": "scalp"} for i in range(3)]
            self.updates = 0

        def find(self, flt, _proj=None):
            return _Cur([r for r in self.rows if r.get("origin") == flt["origin"]])

        async def count_documents(self, flt):
            return sum(1 for r in self.rows if r.get("origin") == flt["origin"])

        async def update_many(self, flt, upd):
            self.updates += 1
            ids = set(flt["_id"]["$in"])                                  # SA7-P3 — bounded by id batch
            self.rows = [({**r, **upd["$set"]} if r["_id"] in ids and r.get("origin") == "scalp" else r) for r in self.rows]
            return _Res()

    class _PS:
        def __init__(self):
            self.calls = []

        async def update_one(self, f, u, upsert=False):
            self.calls.append((f, u))

    db = type("DB", (), {})()
    db.trades, db.platform_state = _Trades(), _PS()
    loop = asyncio.new_event_loop()
    dry = loop.run_until_complete(srm.migrate(db, dry_run=True))
    assert dry == {"legacy": 3, "modified": 0, "dry_run": True} and db.trades.updates == 0
    res = loop.run_until_complete(srm.migrate(db))
    assert res["legacy"] == 3 and res["modified"] == 3 and res["remaining"] == 0 and db.trades.updates == 1
    assert db.platform_state.calls[-1][0] == {"_id": srm.STATE_ID} and db.platform_state.calls[-1][1]["$inc"] == {"modified": 3}
    again = loop.run_until_complete(srm.migrate(db))
    assert again["legacy"] == 0 and again["modified"] == 0 and db.trades.updates == 1                   # nothing left, no second rewrite
    # wiring: startup hook + paper rows carry the engine field
    assert "scalp_rows_migration as _srm" in _read("backend/server.py")
    ex = _read("backend/execution.py")
    paper = ex[ex.index("class PaperEngine"):]
    assert '"engine": signal.get("engine")' in paper and '"scope": signal.get("scope")' not in paper   # N108-2
    assert os.path.exists(os.path.join(ROOT, "scripts", "migrate_scalp_rows.py"))
