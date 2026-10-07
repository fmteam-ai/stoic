"""main106 review:
N106-1 signer init guard fails closed (flyctl error / local key file); N106-2 `enabled` follows the policy
(DEMO-only = every account with trading on) in the panel's "use current" and the acceptance bundle;
N106-3 installation id whitespace + restore-before-install order documented; N106-4 RECOVERED only after a real
heartbeat, never for test accounts, user_id projected; N106-5 a failed pairing check keeps its open alerts."""
import asyncio
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


def _read(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8-sig").read()


def _iso(s):
    return (NOW - timedelta(seconds=s)).isoformat()


# ── N106-1 ───────────────────────────────────────────────────────────────────────────────────────
def test_n106_1_init_guard_fails_closed(tmp_path):
    s = _read("deploy/signer/init_fly_signer.sh")
    assert 'if [ -s "$OUT_DIR/$APP.private.b64" ]' in s                                  # local key file → refuse
    assert 'if ! secrets_out=$(flyctl secrets list -a "$APP" 2>&1); then' in s and s.count("exit 3") >= 4
    assert "flyctl apps list failed" in s and "2>/dev/null | grep -q" not in s           # no hidden flyctl errors
    assert s.index("flyctl apps list failed") < s.index("Ed25519PrivateKey.generate()")
    # behavioural: a fake flyctl whose `secrets list` fails must make the script refuse before minting
    fake = tmp_path / "bin"; fake.mkdir()
    (fake / "flyctl").write_text('#!/usr/bin/env bash\ncase "$1 $2" in\n "auth whoami") exit 0;;\n "apps list") echo \'[{"Name": "stoic-x"}]\';;\n "secrets list") echo "token expired" >&2; exit 1;;\nesac\n')
    (fake / "flyctl").chmod(0o755)
    home = tmp_path / "home"; home.mkdir()
    r = subprocess.run(["bash", os.path.join(ROOT, "deploy", "signer", "init_fly_signer.sh"), "stoic-x", "ams"],
                       env={**os.environ, "PATH": f"{fake}:{os.environ['PATH']}", "HOME": str(home)}, capture_output=True, text=True)
    assert r.returncode == 3 and "refusing rather than risk overwriting" in r.stdout
    assert not (home / ".stoic-signer").exists()                                           # nothing minted


# ── N106-2 ───────────────────────────────────────────────────────────────────────────────────────
def test_n106_2_enabled_follows_the_policy_in_bundle_and_panel():
    import acceptance_bundle as ab
    proj = {"counts": {"configured": 2, "enabled": 2, "live_enabled": 0, "bots_enabled": 2}, "violations": [], "structural_defects": []}
    demo = {"approved_by": "a", "accounts": 2, "enabled": 2, "bots": 2, "demo_only": True}
    assert ab.inventory_failures(proj, demo) == []                                          # DEMO-only: all enabled accounts
    live = {**demo, "demo_only": False}
    fails = ab.inventory_failures(proj, live)
    assert any("enabled accounts 0 != approved 2" in f for f in fails)                       # real-money: LIVE only
    proj2 = {**proj, "counts": {**proj["counts"], "live_enabled": 2}}
    assert ab.inventory_failures(proj2, live) == []
    bad = {**proj, "counts": {**proj["counts"], "bots_enabled": 1}}
    f2 = ab.inventory_failures(bad, demo)
    assert any("enabled accounts (demo-only policy)" not in f and "enabled bots 1 != approved 2" in f for f in f2)
    assert "exactly one enabled bot per enabled account required" in f2
    panel = _read("frontend/src/components/admin/InventoryGoLivePanel.jsx")
    assert "const demoOnly = !!policy?.demo_only;" in panel and "const enabledNow = demoOnly ? c.enabled : c.live_enabled;" in panel
    assert "enabled: String(enabledNow ?? 0)" in panel and 'data-testid="expectation-enabled-semantics"' in panel
    # projection and bundle agree on the demo-only semantics
    ip = _read("backend/inventory_projection.py")
    assert 'counts["enabled"] != exp["enabled"]' in ip[ip.index('if exp.get("demo_only")'):]


# ── N106-3 ───────────────────────────────────────────────────────────────────────────────────────
def test_n106_3_installation_id_whitespace_and_restore_order(tmp_path):
    lib = os.path.join(ROOT, "deploy", "lib.sh")
    (tmp_path / "backend").mkdir(); (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "installation_id").write_text("stoic-abc\n")
    (tmp_path / "backend" / ".env").write_text("STOIC_INSTALLATION_ID=stoic-abc   \r\n")       # padded + CRLF
    r = subprocess.run(["bash", "-c", f". {lib}; ensure_installation_id"], cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 0 and "differs" not in r.stdout
    assert (tmp_path / "backend" / ".env").read_text() == "STOIC_INSTALLATION_ID=stoic-abc\n"   # rewritten clean
    doc = _read("docs/DEPLOYMENT.md")
    assert "Full server rebuild — restore order (N106-3)" in doc and "**before** `deploy/install.sh`" in doc


# ── N106-4 / N106-5 ──────────────────────────────────────────────────────────────────────────────
def test_n106_4_recovered_only_after_a_real_heartbeat_and_never_for_test_accounts():
    import pairing_alerts as pa
    acc_ok = {"_id": "ok", "label": "Demo ok", "installer_paired_at": _iso(900), "last_heartbeat": _iso(5)}
    acc_abandoned = {"_id": "old", "label": "Old", "installer_paired_at": _iso(10 * 86400)}                  # past the 7-day ceiling
    acc_repaired = {"_id": "rep", "label": "Rep", "installer_paired_at": _iso(60), "last_heartbeat": _iso(5000)}  # cleared/re-paired, old hb
    acc_test = {"_id": "t", "label": "Demo t", "user_id": "synthetic-user", "installer_paired_at": _iso(900), "last_heartbeat": _iso(5)}
    open_keys = {pa.dedup_key(k) for k in ("ok", "old", "rep", "t", "deleted")}
    p = pa.plan([acc_ok, acc_abandoned, acc_repaired, acc_test], open_keys, NOW, alert_after=600, ceiling=7 * 86400,
                is_test=lambda a: str(a.get("user_id") or "").startswith("synthetic"))
    assert [k for k, _ in p["recovered"]] == [pa.dedup_key("ok")]            # abandoned / re-paired / deleted / test → silent
    assert p["active"] == set()                                              # they still auto-resolve in ops_alerts
    assert pa.heartbeat_after_pairing(acc_ok) and not pa.heartbeat_after_pairing(acc_repaired) and not pa.heartbeat_after_pairing(acc_abandoned)
    src = _read("backend/pairing_alerts.py")
    assert '"user_id": 1' in src and "is_synthetic_account as is_test" in src    # the test-account check can match


def test_n106_5_failed_check_keeps_open_alerts_out_of_auto_resolve():
    src = _read("backend/alerting.py")
    body = src[src.index("pairing alert evaluation failed"):src.index("# 6 · AUTO-RESOLVE")]
    assert '{"kind": "pairing_no_heartbeat", "acked_at": None}' in body and "active |=" in body

    class _Cur:
        def __init__(self, rows): self.rows = rows
        def __aiter__(self):
            async def g():
                for r in self.rows:
                    yield r
            return g()

    class _Coll:
        def __init__(self, rows): self.rows = rows
        def find(self, *a, **k): return _Cur(self.rows)

    # simulate: the detector raises → the keep-open query must add the open key to `active`
    import pairing_alerts as pa
    db = type("DB", (), {})()
    db.ops_alerts = _Coll([{"dedup_key": pa.dedup_key("x")}])

    async def boom(*a, **k):
        raise RuntimeError("mongo hiccup")

    async def run():
        active = set()
        try:
            await boom()
        except Exception:
            active |= {a["dedup_key"] async for a in db.ops_alerts.find({"kind": pa.KIND, "acked_at": None}, {"dedup_key": 1}) if a.get("dedup_key")}
        return active
    assert asyncio.new_event_loop().run_until_complete(run()) == {pa.dedup_key("x")}
