"""v1.60.61 hotfix - the EA 1.66 release chain failed silently (ea-release run 39030924128 / commit 585d2ce9):
record() crashed on a cp1252 console (U+2192 in a print), `--check` FAILED, pwsh only honoured the LAST exit code,
and the commit step pushed a bare EX5 while both records still said 1.65. Guards: ASCII-safe prints that survive
PYTHONIOENCODING=cp1252, every native command in the record step fails the step, and the commit step refuses an EX5
without its signed record."""
import contextlib
import io
import json
import os
import re
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from unittest.mock import patch  # noqa: E402

from tests.unit.test_v1_60_41_ea_previous_chain import _args, _sign_stable_164, chain  # noqa: E402,F401

pytestmark = pytest.mark.unit
WF = open(os.path.join(ROOT, ".github", "workflows", "ea-release.yml"), encoding="utf-8").read()


def _cp1252_stdout():
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict", write_through=True)


# -- 1. prints never depend on the console encoding -----------------------------------------------------------------
def test_verify_ea_release_source_is_ascii_only_and_hardens_stdout():
    src = open(os.path.join(ROOT, "scripts", "verify_ea_release.py"), encoding="utf-8").read()
    bad = sorted({c for c in src if ord(c) > 127})
    assert not bad, f"non-ASCII characters in verify_ea_release.py: {bad!r}"
    assert '_stream.reconfigure(errors="backslashreplace")' in src
    src2 = open(os.path.join(ROOT, "scripts", "check_ea_commit_consistency.py"), encoding="utf-8").read()
    assert not {c for c in src2 if ord(c) > 127}


def test_record_forward_bump_prints_survive_a_cp1252_console(chain):  # noqa: F811
    c = chain
    _sign_stable_164(c)
    doc = json.load(open(c.hashes)); doc["ea"]["mq5_sha256"] = "0" * 64; json.dump(doc, open(c.hashes, "w"))
    ex5 = c.tmp / "new.ex5"; ex5.write_bytes(b"EX5-1.66")
    out = _cp1252_stdout()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        rc = c.ver.record(_args(c, ex5))                         # the exact path that crashed in run 39030924128
    text = out.buffer.getvalue().decode("cp1252")
    assert rc == 0 and "MQ5 version bumped 1.64 -> " in text and "EX5 recorded - sha256" in text
    assert "UnicodeEncodeError" not in text and "Traceback" not in text
    # the refusal path (same version, different MQ5) prints through SystemExit - also ASCII
    doc = json.load(open(c.hashes)); doc["ea"]["mq5_sha256"] = "0" * 64; json.dump(doc, open(c.hashes, "w"))
    with patch.object(c.ver, "mq5_property_version", lambda path=None: json.load(open(c.rel))["version"]):
        with pytest.raises(SystemExit) as e:
            c.ver.record(_args(c, ex5))
    str(e.value).encode("cp1252")                                 # must not raise


def test_check_under_cp1252_in_an_empty_shell_has_no_traceback(tmp_path):
    manifest = json.load(open(os.path.join(ROOT, "backend", "models_store", "MODEL_MANIFEST.json")))
    envf = tmp_path / "backend.env"
    envf.write_text(f"RELEASE_PUBLIC_KEY_B64={manifest['public_key_b64']}\nRELEASE_SIGNER_KEY_ID={manifest['key_id']}\nRELEASE_SIGNER=external\n")
    r = subprocess.run(["env", "-i", f"PATH={os.environ.get('PATH', '/usr/bin:/bin')}", "PYTHONIOENCODING=cp1252",
                        f"STOIC_BACKEND_ENV_FILE={envf}", sys.executable, os.path.join(ROOT, "scripts", "verify_ea_release.py"), "--check"],
                       capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    assert "Traceback" not in out and "UnicodeEncodeError" not in out, out
    assert out.strip(), "the gate must print its verdict"


# -- 2. workflow lint: the record step fails on ANY failing command, UTF-8 console, commit step guarded -------------
def _step(name: str) -> str:
    i = WF.index(f"- name: {name}")
    j = WF.find("\n      - name: ", i + 1)
    return WF[i:j if j > 0 else len(WF)]


def test_record_step_fails_on_any_failing_native_command_and_forces_utf8():
    step = _step("Record + sign the EX5")
    assert "$ErrorActionPreference = 'Stop'" in step and "$PSNativeCommandUseErrorActionPreference = $true" in step
    assert 'PYTHONUTF8: "1"' in step and 'PYTHONIOENCODING: "utf-8"' in step
    run = step.split("run: |")[1]
    python_calls = [ln for ln in run.splitlines() if ln.strip().startswith("python ")]
    assert len(python_calls) == 3                                    # --sign, --check, fleet gate
    assert run.count("if ($LASTEXITCODE -ne 0) { throw") >= 4        # pip + the three python calls
    for needle in ("--sign", "--check", "check_ea_fleet_compat.py"):
        i = run.index(needle)
        assert "$LASTEXITCODE -ne 0" in run[i:i + 400], needle        # every call is followed by an exit-code check


def test_commit_step_refuses_an_ex5_without_its_record():
    step = _step("Commit the signed record back to the source branch")
    assert "set -euo pipefail" in step
    i_gate = step.index("python scripts/check_ea_commit_consistency.py --staged")
    assert step.index("git add ") < i_gate < step.index("git commit ") < step.index("git push ")


# -- 3. the consistency gate itself ---------------------------------------------------------------------------------
SIGNED_166 = {"version": "1.66", "ex5_sha256": "a" * 64, "signature": {"sig_hex": "s"}}
SIGNED_165 = {"version": "1.65", "ex5_sha256": "b" * 64, "signature": {"sig_hex": "s"}}


def test_gate_rejects_the_585d2ce9_shape_and_accepts_a_consistent_set():
    import check_ea_commit_consistency as G
    bare = G.check([G.EX5], "a" * 64, SIGNED_165, SIGNED_165, "1.66")
    assert any("must never be committed without its record" in p for p in bare)
    assert any("records EA 1.65 but the MQ5 is 1.66" in p for p in bare) and any("!= EX5 on disk" in p for p in bare)
    full = [G.EX5, G.EA_RECORD, G.HASHES]
    assert G.check(full, "a" * 64, SIGNED_166, SIGNED_166, "1.66") == []
    assert any("UNSIGNED" in p for p in G.check(full, "a" * 64, dict(SIGNED_166, signature={}), SIGNED_166, "1.66"))
    assert any("no ex5_sha256" in p for p in G.check(full, "a" * 64, SIGNED_166, {"version": "1.66", "ex5_sha256": None}, "1.66"))
    assert G.check(["release/rc_lock.json"], "a" * 64, SIGNED_165, SIGNED_165, "1.66") == []   # unrelated staging passes
    assert any("missing on disk" in p for p in G.check(full, None, SIGNED_166, SIGNED_166, "1.66"))


def test_gate_cli_exit_codes_in_a_scratch_repo(tmp_path):
    for sub in ("backend/static", "release", "docs"):
        os.makedirs(tmp_path / sub)
    (tmp_path / "backend/static/EmergentTradingBridge.mq5").write_text('#property version   "1.66"\n')
    (tmp_path / "backend/static/EmergentTradingBridge.ex5").write_bytes(b"EX5")
    import hashlib
    sha = hashlib.sha256(b"EX5").hexdigest()
    json.dump(SIGNED_165, open(tmp_path / "release/ea_release.json", "w"))
    json.dump({"ea": SIGNED_165}, open(tmp_path / "docs/RELEASE_HASHES.json", "w"))
    script = os.path.join(ROOT, "scripts", "check_ea_commit_consistency.py")
    r = subprocess.run([sys.executable, script, "--repo", str(tmp_path), "--files", "backend/static/EmergentTradingBridge.ex5"],
                       capture_output=True, text=True, env={**os.environ, "PYTHONIOENCODING": "cp1252"})
    assert r.returncode == 1 and "REFUSED" in r.stdout and "Nothing committed" in r.stdout and "Traceback" not in r.stderr
    good = dict(SIGNED_166, ex5_sha256=sha)
    json.dump(good, open(tmp_path / "release/ea_release.json", "w"))
    json.dump({"ea": good}, open(tmp_path / "docs/RELEASE_HASHES.json", "w"))
    r = subprocess.run([sys.executable, script, "--repo", str(tmp_path), "--files", "backend/static/EmergentTradingBridge.ex5",
                        "release/ea_release.json", "docs/RELEASE_HASHES.json"], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.startswith("OK:")
    assert re.search(r"[^\x00-\x7f]", r.stdout) is None


# -- security audit #39 P3: ForexFactory XML - size cap + no DOCTYPE/ENTITY before stdlib ElementTree parses it -------
def test_calendar_feed_rejects_entity_expansion_and_oversize_but_parses_the_real_shape():
    import economic_calendar as EC
    bomb = (b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;">]>'
            b"<weeklyevents><event><title>&lol2;</title></event></weeklyevents>")
    assert EC.parse_feed_xml(bomb) is None
    assert EC.parse_feed_xml(b"<weeklyevents>" + b"<event/>" * 10 + b"</weeklyevents>" + b" " * EC.FF_FEED_MAX_BYTES) is None
    assert EC.parse_feed_xml(b"<weeklyevents><event><title>CPI y/y") is None            # malformed -> None, no raise
    root = EC.parse_feed_xml(b"<weeklyevents><event><title>CPI y/y</title><country>USD</country><impact>High</impact>"
                             b"<date>10-17-2026</date><time>8:30am</time><forecast>3.1%</forecast><previous>3.0%</previous>"
                             b"<actual></actual></event></weeklyevents>")
    assert root is not None and root.findtext("event/title") == "CPI y/y"
    src = open(os.path.join(ROOT, "backend", "economic_calendar.py"), encoding="utf-8").read()
    body = src.split("async def _fetch_events(")[1].split("\n    events = []")[0]
    assert "root = parse_feed_xml(raw)" in body and "ET.fromstring(" not in body
