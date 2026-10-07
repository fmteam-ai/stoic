"""main108 review: N108-1 workflow deps (pymongo) + clean-env import step; N108-2 no `scope` on paper rows;
N108-3 SIGNED EX5 button shows 'not published yet' when absent; N108-4 no signed policy → deployment version;
N108-5 wording / defaults / ceil rounding / ack suppression / banner fail-closed."""
import asyncio
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


def _read(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def test_n108_1_workflow_installs_the_signer_dependencies():
    wf = _read(".github/workflows/policy-migration.yml")
    assert "pip install -q cryptography==50.0.0 requests==2.34.2 pymongo==4.18.2" in wf
    assert "Signer imports in a clean environment (N108-1)" in wf and "sign_policy_migration.py --help" in wf
    assert wf.index("Signer imports in a clean environment") < wf.index("python scripts/sign_policy_migration.py \\")
    # the import chain the script needs really is just these
    import subprocess
    r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "sign_policy_migration.py"), "--help"], capture_output=True, text=True)
    assert r.returncode == 0 and "--demo-only" in r.stdout


def test_n108_2_paper_rows_carry_engine_but_no_scope():
    ex = _read("backend/execution.py")
    paper = ex[ex.index("class PaperEngine"):]
    assert '"engine": signal.get("engine")' in paper and '"scope": signal.get("scope")' not in paper
    assert '{"scope": "scalp_fast", "status": "open"}' in _read("backend/scalp/engine.py")   # the sweep this protects


def test_n108_3_signed_ex5_button_says_not_published_until_available():
    acc = _read("frontend/src/pages/Accounts.jsx")
    assert "ex5Available" in acc and 'data-testid="download-ex5-unavailable"' in acc and "not published yet" in acc
    assert 'data-testid="download-ex5-button"' in acc
    assert "/ea-script.ex5" in acc and "method: \"HEAD\"" in acc


def test_n108_4_no_signed_policy_resets_to_the_deployment_version():
    import inventory_projection as ip
    v = ip.validate_expectation({"accounts": 6, "enabled": 3, "bots": 3, "account_ids": ["a", "b", "c"]},
                                require_policy=False, current_policy_version="demo-2x2-v1")
    assert v["policy_version"] == ip.DEPLOYMENT_POLICY_VERSION and v["demo_only"] is False   # not "demo-2x2-v1"


def test_n108_5_wording_defaults_rounding_ack_suppression_banner():
    import inventory_projection as ip
    assert "closed testing period" in _read("backend/signup_lock.py")
    wf = _read(".github/workflows/policy-migration.yml")
    assert 'IN_EXPIRES_DAYS=30; else IN_EXPIRES_DAYS=45' in wf and 'default: ""' in wf           # same defaults as the script
    pe = ip.policy_expiry({"policy_expires_at": (NOW + timedelta(days=2, hours=21)).isoformat()}, NOW)
    assert pe["days_left"] == 3 and pe["reminder_due"]                                              # 2.9 d → 3 days
    assert ip.policy_expiry({"policy_expires_at": (NOW + timedelta(days=3, hours=1)).isoformat()}, NOW)["reminder_due"] is False
    banner = _read("frontend/src/components/ClosedBetaBanner.jsx")
    assert "setS({ closed: true, message: \"\" })" in banner and ".catch(() => setS(null))" not in banner
    # ack suppression: an operator ack within 6 h stops the re-raise; a system auto-resolve does not
    import alerting
    assert alerting.ACK_SUPPRESS_S == 6 * 3600

    class _Alerts:
        def __init__(self, acked_by, acked_at):
            self.acked_by, self.acked_at, self.inserted = acked_by, acked_at, 0

        async def find_one(self, flt, sort=None, projection=None):
            if flt.get("acked_at") is None:
                return None
            if "$not" in flt.get("acked_by", {}) and re.match(flt["acked_by"]["$not"]["$regex"], self.acked_by):
                return None
            return {"acked_at": self.acked_at}

        async def insert_one(self, doc):
            self.inserted += 1
            return type("R", (), {"inserted_id": "x"})()

        async def update_one(self, *a, **k):
            return None

    async def run(acked_by, acked_at):
        db = type("DB", (), {})()
        db.ops_alerts = _Alerts(acked_by, acked_at)
        rid = await alerting.raise_alert(db, "policy_expired", "critical", "msg", dedup_key="policy_expiry:expired", meta={})
        return rid, db.ops_alerts.inserted

    loop = asyncio.new_event_loop()
    assert loop.run_until_complete(run("admin@x", datetime.now(timezone.utc) - timedelta(hours=1))) == (None, 0)      # suppressed
    rid, n = loop.run_until_complete(run("admin@x", datetime.now(timezone.utc) - timedelta(hours=7)))
    assert rid is not None and n == 1                                                                               # window over → re-raised
    rid, n = loop.run_until_complete(run("system:auto-resolved", datetime.now(timezone.utc) - timedelta(minutes=1)))
    assert rid is not None and n == 1                                                                               # auto-resolve never suppresses
