"""External audit follow-up (14 Oct): P1-04 policy on strip + /status · P1-06/07 claims · P2-07 docs/CI · release-gate lock."""
import asyncio
import json
import os
import re
import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(BACKEND)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _src(*p):
    return open(os.path.join(ROOT, *p), encoding="utf-8").read()


# ── P1-06 / P1-07 — no unproven performance claims, honest Manual Trade wording ───────────────────────────────
def test_no_performance_claims_or_steady_wealth_tagline():
    guide = _src("frontend", "src", "pages", "Guide.jsx")
    assert "55–65% win rate" not in guide and "6–12% monthly" not in guide
    assert "bypasses every veto" not in guide
    assert "paper-only manual test" in guide and "server-side risk, account, rate and execution controls still apply" in guide
    for f in ("activation.py", "password_reset.py"):
        assert "STEADY WEALTH" not in _src("backend", f), f
    assert "steady wealth" not in _src("frontend", "index.html").lower()
    trailer = _src("frontend", "src", "pages", "WelcomeTrailer.jsx")
    assert "85%" not in trailer and "85 percent" not in trailer and "Broker risk disclosures" in trailer
    # repo-wide: no "% win rate" / "% monthly" promises in user-facing UI or e-mail templates
    bad = []
    for base, _, files in os.walk(os.path.join(ROOT, "frontend", "src")):
        for n in files:
            if n.endswith((".jsx", ".js")):
                s = open(os.path.join(base, n), encoding="utf-8").read()
                if re.search(r"\d+\s*[–-]\s*\d+%\s*(win rate|monthly)", s) or re.search(r"~\d+%\s*monthly", s):
                    bad.append(n)
    assert not bad, bad


# ── P1-04 — active policy on the readiness strip and /status ───────────────────────────────────────────────────
def test_active_policy_summary_and_surfaces():
    from policy_summary import active_policy_summary
    db = FakeDb()
    with patch("rules_policy.active", AsyncMock(return_value={"policy_version": "demo-rules-v2", "demo_only": True,
                                                              "expires_at": "2026-12-01T00:00:00+00:00"})):
        s = _run(active_policy_summary(db))
    assert s["policy_version"] == "demo-rules-v2" and s["scope"] == "demo_only" and s["label"] == "demo-rules-v2 · DEMO-ONLY"
    assert "6/3/3" in s["live_target"]
    with patch("rules_policy.active", AsyncMock(return_value=None)):
        s = _run(active_policy_summary(db))
    assert s["policy_version"] is None and "NO ACTIVE POLICY" in s["label"]
    with patch("rules_policy.active", AsyncMock(return_value={"policy_version": "demo-rules-v2", "demo_only": True, "expired": True})):
        assert "EXPIRED" in _run(active_policy_summary(db))["label"]
    assert '"policy": await _policy(db)' in _src("backend", "trading_readiness.py")
    assert '"policy": policy,' in _src("backend", "routes", "bot_routes.py")
    strip = _src("frontend", "src", "components", "TradingReadinessStrip.jsx")
    assert "trading-readiness-policy" in strip and "POLICY {data.policy.label}" in strip


# ── P2-07 — docs reflect production, no stale EA-version references in OPEN findings / current-state ──────────
def _latest_ea() -> str:
    from ea_capabilities import latest_ea_version
    return str(latest_ea_version())


def test_open_findings_and_checklist_current_state_carry_no_stale_ea_versions():
    latest = _latest_ea()
    assert re.match(r"^1\.\d\d$", latest), latest
    items = json.loads(_src("docs", "open_findings.json"))
    assert all(f.get("id") != "SEC-002-r21-old-live-build" for f in items)          # closed: v1.60.32 live and healthy
    stale = []
    for f in items:
        for m in re.finditer(r"EA v?(1\.\d\d)\b", json.dumps(f, ensure_ascii=False)):
            if m.group(1) != latest:
                stale.append((f.get("id"), m.group(0)))
    assert not stale, f"open_findings.json references EA versions other than the shipped {latest}: {stale}"
    p13 = next(f for f in items if f["id"] == "P1-03-r18")
    assert "demo-rules-v2" in p13["title"] and "FUTURE live-capital policy target" in p13["title"]
    assert "rs0" in p13["production_state_2026_10_14"] and f"EA {latest}" in p13["production_state_2026_10_14"]
    doc = _src("docs", "PRODUCTION_DEPLOY_CHECKLIST.md")
    assert "## Current production state" in doc and "demo-rules-v2 (demo_only = true)" in doc and "rs0" in doc
    # everything BEFORE the first historical "## EA v1.xx" section must not pin an old EA version
    head = doc.split("\n## EA v1.")[0]
    stale_doc = [m.group(0) for m in re.finditer(r"EA v?(1\.\d\d)\b", head) if m.group(1) != latest]
    assert not stale_doc, f"PRODUCTION_DEPLOY_CHECKLIST current-state section pins old EA versions: {stale_doc}"


# ── release-gate packaging — the gate reads the adopted lock of THIS build ─────────────────────────────────────
def test_release_gate_honours_rc_lock_path_and_checks_build_binding(tmp_path, monkeypatch):
    import release_gate as rg
    lock = {"authoritative": True, "git_commit": "35164315abcdef", "images": {"backend": "sha256:b", "frontend": "sha256:f"}}
    p = tmp_path / "rc_lock.json"
    p.write_text(json.dumps(lock), encoding="utf-8")
    monkeypatch.setenv("RC_LOCK_PATH", str(p))
    assert rg.load_lock() == lock
    good = rg.lock_matches_build(lock, build_sha="35164315abcdef0000")
    assert good["ok"] and good["path"] == str(p)
    bad = rg.lock_matches_build(lock, build_sha="0ee005f0deadbeef")
    assert not bad["ok"] and "rc_lock.git_commit != BUILD_SHA" in bad["reason"]
    unknown = rg.lock_matches_build(lock, build_sha="unknown")
    assert not unknown["ok"] and unknown["reason"] == "lock or BUILD_SHA unknown"
    # the gate itself (reading the lock from disk, as production does) refuses a lock from a different build
    with patch("modules.pamm.strategy_guard.GIT_COMMIT", "0ee005f0deadbeef"):
        res = rg.evaluate(env={"STOIC_IMAGE_DIGEST": "sha256:b"}, ea_signed=True)
    assert not res["ok"] and any("rc_lock.git_commit != BUILD_SHA" in f for f in res["failures"])
    with patch("modules.pamm.strategy_guard.GIT_COMMIT", "35164315abcdef"):
        res = rg.evaluate(env={"STOIC_IMAGE_DIGEST": "sha256:b"}, ea_signed=True)
    assert res["ok"], res["failures"]
    assert res["lock_binding"]["ok"] and res["lock_binding"]["path"] == str(p)
    compose = _src("docker-compose.yml")
    assert compose.count("./release/rc_lock.json:/app/release/rc_lock.json:ro") == 2 and "RC_LOCK_PATH: /app/release/rc_lock.json" in compose
    upd = _src("deploy", "update.sh")
    assert "lock_matches_build" in upd and 'r["ok"] and r["authoritative"]' in upd
    assert "lock_matches_build" in _src("backend", "server.py")
