"""AI latency card + EA release-hash drift guard.
Pure unit tests (fake async db) — run with DB_NAME="".
"""
import asyncio
import inspect
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "unit"))
from fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _Chat:
    provider, model = "openai", "gpt-5-mini"

    def __init__(self, delay=0.0, fail=False):
        self.delay, self.fail = delay, fail

    async def send_message(self, msg):
        await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("provider down")
        return "ok"


def test_every_llm_call_is_sampled_with_outcome():
    import llm_timeout as lt
    db = FakeDb()
    with patch("database.get_db", lambda: db):
        assert run(lt.send_with_timeout(_Chat(), "m", label="narration:XAUUSD")) == "ok"
        with pytest.raises(asyncio.TimeoutError):
            run(lt.send_with_timeout(_Chat(delay=0.3), "m", label="sentiment:EURUSD", seconds=0.05))
        with pytest.raises(RuntimeError):
            run(lt.send_with_timeout(_Chat(fail=True), "m", label="bot_doctor"))
    rows = db.ai_latency_samples.rows
    assert [r["outcome"] for r in rows] == ["ok", "timeout", "error"]
    assert rows[0]["label"] == "narration" and rows[0]["provider"] == "openai" and rows[0]["model"] == "gpt-5-mini"
    assert rows[1]["ms"] >= 50 and rows[1]["timeout_s"] == 0.05


def test_latency_summary_verdicts_slow_failing_healthy():
    import llm_timeout as lt
    db = FakeDb()
    now = datetime.now(timezone.utc).isoformat()

    def s(provider, model, ms, outcome="ok", label="narration"):
        db.ai_latency_samples.rows.append({"provider": provider, "model": model, "ms": ms, "outcome": outcome, "label": label, "at": now})
    for ms in (800, 900, 1000, 1100, 1200):
        s("openai", "fast", ms)
    for ms in (7000, 8000, 9000, 9500):
        s("anthropic", "deep", ms)
    for i in range(4):
        s("gemini", "flash", 500, outcome="timeout" if i < 3 else "ok")
    s("openai", "rare", 100)
    with patch.dict(os.environ, {"AI_CALL_TIMEOUT_SEC": "10"}):
        out = run(lt.latency_summary(db, hours=24))
    by = {(p["provider"], p["model"]): p for p in out["providers"]}
    assert by[("openai", "fast")]["verdict"] == "healthy" and by[("openai", "fast")]["p95_ms"] == 1200
    assert by[("anthropic", "deep")]["verdict"] == "slow" and "p95" in by[("anthropic", "deep")]["why"]
    assert by[("gemini", "flash")]["verdict"] == "failing" and by[("gemini", "flash")]["timeout"] == 3
    assert by[("openai", "rare")]["verdict"] == "insufficient_data"
    assert out["overall"] == "failing" and out["providers"][0]["provider"] == "gemini"      # worst first
    assert out["samples"] == 14 and out["timeout_s"] == 10.0


def test_ai_latency_endpoint_and_card_wired():
    import routes.diagnostic_routes as dr
    assert '@router.get("/ai-latency")' in inspect.getsource(dr)
    from security_matrix import BOLA_MATRIX
    assert BOLA_MATRIX[("GET", "/api/diagnostic/ai-latency")] == "admin_only"       # audit P3
    assert "require_admin(user)" in inspect.getsource(dr.ai_latency_view) and "require_admin(user)" in inspect.getsource(dr.fx_rates_view)
    fe = os.path.join(ROOT, "frontend", "src")
    assert "ai-latency-card" in open(os.path.join(fe, "components", "AiLatencyCard.jsx")).read()
    assert "<AiLatencyCard />" in open(os.path.join(fe, "pages", "BotHealth.jsx")).read()


# ── release-hash drift guard ────────────────────────────────────────────────
def test_drift_guard_passes_on_repo_and_fails_on_drift(tmp_path):
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import check_release_hash_drift as g
    assert g.check() == []                                               # the repo itself is consistent
    mq5 = tmp_path / "EA.mq5"; mq5.write_text('#property version   "1.58"\nint x;\n')
    hashes = tmp_path / "RELEASE_HASHES.json"
    hashes.write_text(json.dumps({"ea": {"version": "1.58", "mq5_sha256": g.sha256_file(str(mq5))}}))
    assert g.check(str(mq5), str(hashes), str(tmp_path / "none.json")) == []
    mq5.write_text('#property version   "1.58"\nint y;\n')                # source changed, hash not re-captured
    fails = g.check(str(mq5), str(hashes), str(tmp_path / "none.json"))
    assert len(fails) == 1 and "drifted" in fails[0] and "capture_release_hashes" in fails[0]
    hashes.write_text(json.dumps({"ea": {"version": "1.57", "mq5_sha256": g.sha256_file(str(mq5))}}))
    assert any("version 1.57 != MQ5" in f for f in g.check(str(mq5), str(hashes), str(tmp_path / "none.json")))
    rel = tmp_path / "ea_release.json"
    rel.write_text(json.dumps({"ex5_sha256": "f" * 64, "mq5_sha256": "0" * 64}))
    hashes.write_text(json.dumps({"ea": {"version": "1.58", "mq5_sha256": g.sha256_file(str(mq5))}}))
    assert any("DIFFERENT MQ5" in f for f in g.check(str(mq5), str(hashes), str(rel)))


def test_drift_guard_runs_in_the_first_ci_job():
    ci = open(os.path.join(ROOT, ".github", "workflows", "ci.yml")).read()
    assert "python scripts/check_release_hash_drift.py" in ci
    assert ci.index("check_release_hash_drift.py") < ci.index("ea-compile:")
    out = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "check_release_hash_drift.py")], capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.startswith("OK:")


def test_mq5_hash_is_line_ending_independent(tmp_path):
    """The Windows MetaEditor runner checks the MQ5 out with CRLF; every hash in the release
    chain (record, check, drift guard, server pin) must still equal the recorded LF hash."""
    import hashlib
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import check_release_hash_drift as g
    import verify_ea_release as v
    import capture_release_hashes as c
    import ea_capabilities as ec
    src = open(os.path.join(ROOT, "backend", "static", "EmergentTradingBridge.mq5"), "rb").read()
    assert b"\r\n" not in src
    crlf = tmp_path / "EA.mq5"; crlf.write_bytes(src.replace(b"\n", b"\r\n"))
    lf_hash = hashlib.sha256(src).hexdigest()
    recorded = json.load(open(os.path.join(ROOT, "docs", "RELEASE_HASHES.json")))["ea"]["mq5_sha256"]
    assert lf_hash == recorded
    assert g.sha256_file(str(crlf)) == v.sha256_source(str(crlf)) == c.sha256_source(str(crlf)) == lf_hash
    assert hashlib.sha256(crlf.read_bytes().replace(b"\r\n", b"\n")).hexdigest() == lf_hash   # ea_capabilities pin
    assert "backend/static/EmergentTradingBridge.mq5 text eol=lf" in open(os.path.join(ROOT, ".gitattributes")).read()
    assert ec.shipped_ea_version() == "1.58"
