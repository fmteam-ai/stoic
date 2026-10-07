"""A16-5 demo evidence types + real/contest alert, A15-7 download labels, A16-6 Playwright chart provenance spec."""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _read(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


def _demo_account(**kw):
    from broker_env import attestation_identity
    acc = {"_id": "a1", "label": "Demo A", "broker": "ICM", "account_number": 5012345, "mode": "live", "server": "ICMarkets-Demo",
           "broker_server": "ICMarkets-Demo", "broker_environment": "DEMO", "account_type": "demo",
           "ea_identity": {"authoritative": True, "installation_id": "inst_1", "broker_server": "ICMarkets-Demo", "ea_version": "1.60"},
           "broker_account_id_reported": "5012345", **kw}
    acc["environment_attestation"] = {"environment": "DEMO", "approved_by": "admin@x", "identity_hash": attestation_identity(acc),
                                      "proof": {"verifier": kw.get("_verifier", "ea_heartbeat")}}
    return acc


def test_demo_evidence_types():
    import demo_readiness as dr
    # broker reported DEMO + signed EX5
    a = _demo_account(account_trade_mode="demo", ea_binary_sha256="a" * 64, ea_binary_sha256_method="installer_attested")
    from unittest.mock import patch
    with patch("broker_env.ea_binary_accepted", return_value=True):
        ev = dr.demo_evidence(a)
    assert ev["kind"] == "broker_signed" and "signed EX5" in ev["label"] and ev["binary"] == "signed"
    # broker reported DEMO but locally compiled binary
    a = _demo_account(account_trade_mode="demo", ea_binary_sha256="b" * 64, ea_binary_sha256_method="reported")
    with patch("broker_env.ea_binary_accepted", return_value=False):
        ev = dr.demo_evidence(a)
    assert ev["kind"] == "broker_demo_binary" and "demo-only binary" in ev["label"] and ev["binary"] == "local"
    # server-name rule (EA < 1.60, no broker mode)
    a = _demo_account()
    a["ea_identity"]["ea_version"] = "1.58"
    with patch("broker_env.ea_binary_accepted", return_value=False):
        ev = dr.demo_evidence(a)
    assert ev["kind"] == "server_name" and ev["binary"] == "unknown"
    # admin override
    a = _demo_account(_verifier="admin_override")
    with patch("broker_env.ea_binary_accepted", return_value=False):
        ev = dr.demo_evidence(a)
    assert ev["kind"] == "admin_override" and ev["verifier"] == "admin_override"
    # broker says real → attestation void, red label
    a = _demo_account(account_trade_mode="real")
    with patch("broker_env.ea_binary_accepted", return_value=False):
        ev = dr.demo_evidence(a)
    assert ev["kind"] == "real_money" and "REAL/CONTEST" in ev["label"]
    # changing the server name voids the attestation (identity hash no longer matches) → not DEMO, trading on it stops
    from broker_env import attested_environment
    a = _demo_account(account_trade_mode="demo")
    assert attested_environment(a) == "DEMO"
    a["server"] = "ICMarkets-Live2"
    assert attested_environment(a) == "LIVE"
    with patch("broker_env.ea_binary_accepted", return_value=False):
        ev = dr.demo_evidence(a)
    assert ev["kind"] == "none" and "invalidated" in ev["label"]
    ui = _read("frontend/src/pages/DemoReadiness.jsx")
    assert "demo-evidence-${r.id}" in ui and "demo-binary-${r.id}" in ui and "<th className=\"pr-3\">EVIDENCE</th>" in ui


def test_demo_account_reporting_real_raises_a_loud_alert():
    import demo_mode_alerts as dm
    good = _demo_account(account_trade_mode="demo")
    bad = _demo_account(account_trade_mode="contest")
    bad["_id"] = "a2"
    unattested = {"_id": "a3", "account_trade_mode": "real"}
    p = dm.plan([good, bad, unattested])
    assert p["active"] == {dm.dedup_key("a2")}
    key, text, meta, acc = p["raise"][0]
    assert "REPORTS REAL MONEY" in text and "'contest'" in text and "…345" in text and "5012345" not in text
    sent, raised = [], []

    async def notify(t):
        sent.append(t)

    async def raise_alert(db, kind, sev, msg, dedup_key=None, meta=None, synthetic=False):
        raised.append((kind, sev, dedup_key, synthetic))
        return "new"

    class _Cur:
        def __init__(self, rows): self.rows = rows
        def limit(self, _n): return self
        def __aiter__(self):
            async def g():
                for r in self.rows:
                    yield r
            return g()

    class _Coll:
        def __init__(self, rows): self.rows = rows
        def find(self, *a, **k): return _Cur(self.rows)

    db = type("DB", (), {})()
    db.accounts = _Coll([good, bad])
    active, n = asyncio.new_event_loop().run_until_complete(dm.evaluate(db, raise_alert=raise_alert, notify=notify))
    assert active == {dm.dedup_key("a2")} and n == 1 and raised == [(dm.KIND, "critical", dm.dedup_key("a2"), False)] and len(sent) == 1
    import alerting
    assert dm.KIND in alerting.EVALUATOR_KINDS and "demo_mode_alerts.evaluate(db, now, raise_alert=raise_alert)" in _read("backend/alerting.py")


def test_download_labels_and_provenance_spec():
    acc = _read("frontend/src/pages/Accounts.jsx")
    assert 'data-testid="download-ea-button"' in acc and "MQ5 SOURCE v{LATEST_EA_VERSION}" in acc and "compile yourself" in acc
    assert 'data-testid="download-ex5-button"' in acc and "SIGNED EX5" in acc and "ea-script.ex5" in acc and "install this" in acc
    assert "DOWNLOAD EA v{LATEST_EA_VERSION}" not in acc
    spec = _read("e2e/tests/chart_provenance.spec.ts")
    for case in ("fresh broker-reconciled", "stale derived", "missing provenance", "conflicting", "partial data", "simulated"):
        assert case in spec, case
    for tid in ("analytics-provenance-kind", "analytics-provenance-missing", "analytics-provenance-gaps", "analytics-provenance-fallback", "analytics-provenance-freshness"):
        assert tid in spec, tid
    prov = _read("frontend/src/components/ChartProvenance.jsx")
    for tid in ("-kind", "-missing", "-gaps", "-fallback", "-freshness"):
        assert f"${{testid}}{tid}" in prov, tid
