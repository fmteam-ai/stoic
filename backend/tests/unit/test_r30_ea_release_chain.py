"""P1-03/P1-04 — sanctioned EX5 chain: record → sign → bind; live gate trusts
only a signed, CI-compiled, source-matching record."""
import base64
import hashlib
import json
import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(ROOT, "backend"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

LOG_OK = "MetaEditor 5.00 build 4620\nResult: 0 errors, 2 warnings\n"


@pytest.fixture
def chain(tmp_path, monkeypatch):
    import verify_ea_release as v
    import ea_capabilities as ec
    monkeypatch.setenv("RELEASE_SIGNER", "local")
    monkeypatch.setenv("ED25519_SIGNING_KEY_B64", base64.b64encode(b"\x07" * 32).decode())
    monkeypatch.delenv("RELEASE_PUBLIC_KEY_B64", raising=False)
    monkeypatch.delenv("EA_RELEASE_SHA256", raising=False)
    monkeypatch.setenv("APP_ENV", "development")
    hashes = tmp_path / "RELEASE_HASHES.json"
    hashes.write_text(json.dumps({"ea": {"version": "x"}}))
    rel = tmp_path / "ea_release.json"
    monkeypatch.setattr(v, "HASHES", str(hashes))
    monkeypatch.setattr(v, "EA_RELEASE", str(rel))
    monkeypatch.setattr(v, "RC_LOCK", str(tmp_path / "rc_lock.json"))
    monkeypatch.setattr(ec, "_EA_RELEASE_FILES", (str(rel),))
    ec._EXPECTED_CACHE.clear()
    ex5 = tmp_path / "EmergentTradingBridge.ex5"
    ex5.write_bytes(b"EX5" * 100)
    log = tmp_path / "compile.log"
    log.write_bytes(("\ufeff" + LOG_OK).encode("utf-16"))      # MetaEditor writes UTF-16
    return {"v": v, "ec": ec, "rel": rel, "hashes": hashes, "ex5": ex5, "log": log}


def _args(chain, **over):
    import argparse
    d = {"ex5": str(chain["ex5"]), "compile_log": str(chain["log"]), "metaeditor_version": None,
         "windows_build": "10.0.22631", "mt5_build": None, "sign": True, "source_commit": "a" * 40,
         "compiled_by": "github-actions"}
    d.update(over)
    return argparse.Namespace(**d)


def test_record_reads_toolchain_from_utf16_log_and_signs(chain):
    v, ec = chain["v"], chain["ec"]
    assert v.record(_args(chain)) == 0
    rec = json.load(open(chain["rel"]))
    assert rec["version"] == v.mq5_property_version() == ec.version_str(ec.LIVE_MIN_VERSION)
    assert rec["metaeditor_version"] == "5.00 build 4620" and rec["mt5_build"] == "4620"
    assert rec["compile_log"]["errors"] == 0 and rec["source_commit"] == "a" * 40
    assert rec["compiled_by"] == "github-actions" and rec["signature"]["sig_hex"]
    assert rec["ex5_sha256"] == hashlib.sha256(chain["ex5"].read_bytes()).hexdigest()
    assert v.check_entry(rec) == []
    # the live gate now has a pinned, trusted hash
    assert ec.expected_ea_sha256() == rec["ex5_sha256"]


def test_live_gate_refuses_unsigned_manual_or_drifted_records(chain):
    v, ec = chain["v"], chain["ec"]
    v.record(_args(chain))
    good = json.load(open(chain["rel"]))

    def _set(rec):
        chain["rel"].write_text(json.dumps(rec)); ec._EXPECTED_CACHE.clear()
        os.utime(chain["rel"], None)

    _set({**good, "signature": None});                       assert ec.expected_ea_sha256() is None
    _set({**good, "compiled_by": "manual"});                 assert ec.expected_ea_sha256() is None
    _set({**good, "mq5_sha256": "0" * 64});                  assert ec.expected_ea_sha256() is None   # source drift
    tampered = {**good, "ex5_sha256": "f" * 64}              # hash swapped after signing → signature fails
    _set(tampered);                                          assert ec.expected_ea_sha256() is None
    _set({**good, "version": "9.99"});                       assert ec.expected_ea_sha256() is None
    _set(good);                                              assert ec.expected_ea_sha256() == good["ex5_sha256"]
    assert "sanctioned CI MetaEditor job" in " ".join(v.check_entry({**good, "compiled_by": "manual"}))
    assert any("version" in f for f in v.check_entry({**good, "version": "9.99"}))


def test_record_refuses_compile_errors_and_log_mismatch(chain):
    v = chain["v"]
    chain["log"].write_text("MetaEditor 5.00 build 4620\nResult: 2 errors, 0 warnings\n")
    with pytest.raises(SystemExit):
        v.record(_args(chain))
    chain["log"].write_text(LOG_OK)
    with pytest.raises(SystemExit):
        v.record(_args(chain, metaeditor_version="5.00 build 1"))   # CLI claim contradicts the log


def test_ci_chain_is_wired():
    wf = open(os.path.join(ROOT, ".github", "workflows", "ea-release.yml")).read()
    for needle in ("metaeditor64.exe", "--compiled-by github-actions --sign", "verify_ea_release.py --check",
                   "git add release/ea_release.json", "RELEASE_SIGNER_URL"):
        assert needle in wf, needle
    rel = open(os.path.join(ROOT, ".github", "workflows", "release.yml")).read()
    assert "recorded EX5" in rel and "ea_release.json" in rel and "EA_RELEASE_SHA256=" in rel
    assert "release/ea_release.jso[n]" in open(os.path.join(ROOT, "Dockerfile.backend")).read()
