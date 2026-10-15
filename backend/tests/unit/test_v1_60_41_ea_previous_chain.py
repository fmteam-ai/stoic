"""v1.60.41 — the signed `previous` EA record must survive a release (14 Oct incident).

A merge replaced docs/RELEASE_HASHES.json#ea (signed 1.64) with an unsigned 1.65 capture; ea-release then signed
1.65 with previous: null; the server rejected the stable 1.64 EX5 on all 4 terminals, voided their DEMO
attestations and put every account into CLOSE_ONLY. Covered here:
  · verify_ea_release.record() carries the signed prior release from release/ea_release.json (or #ea / #ea.previous)
    and FAILS CLOSED when the version changed and no signed prior record exists (never previous: null silently);
  · capture_release_hashes never discards a signed ea entry (carried as `previous`);
  · scripts/check_ea_fleet_compat.py blocks a record that does not accept the fleet's stable EX5;
  · deploying a new EA version while terminals still report the previous signed EX5 keeps the accounts
    attested DEMO and FULL (and the incident shape — previous: null — is what voids them)."""
import asyncio
import base64
import json
import os
import sys
import types
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from bson import ObjectId

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(ROOT, "backend"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
pytestmark = pytest.mark.unit

LOG_OK = "MetaEditor 5.00 build 4620\nResult: 0 errors, 0 warnings\n"
STABLE_164 = b"EX5-1.64-stable"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _local_signer_env():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization as s
    k = Ed25519PrivateKey.generate()
    priv = base64.b64encode(k.private_bytes(s.Encoding.Raw, s.PrivateFormat.Raw, s.NoEncryption())).decode()
    pub = base64.b64encode(k.public_key().public_bytes(s.Encoding.Raw, s.PublicFormat.Raw)).decode()
    return {"RELEASE_SIGNER": "local", "APP_ENV": "preview", "RELEASE_SIGNER_DEFERRED": "false",
            "ED25519_SIGNING_KEY_B64": priv, "RELEASE_PUBLIC_KEY_B64": pub, "GITHUB_ACTIONS": "true", "GITHUB_SHA": "c" * 40}


@pytest.fixture
def chain(tmp_path, monkeypatch):
    import verify_ea_release as ver
    import ea_capabilities as ec
    for k, v in _local_signer_env().items():
        monkeypatch.setenv(k, v)
    for k in ("EA_RELEASE_SHA256", "EA_RELEASE_SHA256_PREVIOUS", "RELEASE_SIGNER_KEY_ID"):
        monkeypatch.delenv(k, raising=False)
    hashes = tmp_path / "RELEASE_HASHES.json"
    rel = tmp_path / "release" / "ea_release.json"
    monkeypatch.setattr(ver, "HASHES", str(hashes))
    monkeypatch.setattr(ver, "EA_RELEASE", str(rel))
    monkeypatch.setattr(ver, "RC_LOCK", str(tmp_path / "rc_lock.json"))
    monkeypatch.setattr(ec, "_EA_RELEASE_FILES", (str(rel),))
    ec._EXPECTED_CACHE.clear()
    log = tmp_path / "compile.log"
    log.write_bytes(("\ufeff" + LOG_OK).encode("utf-16"))
    return types.SimpleNamespace(ver=ver, ec=ec, hashes=hashes, rel=rel, log=log, tmp=tmp_path)


def _args(c, ex5, **over):
    d = {"ex5": str(ex5), "compile_log": str(c.log), "metaeditor_version": None, "windows_build": "10.0.26100",
         "mt5_build": None, "sign": True, "source_commit": "a" * 40, "compiled_by": "github-actions", "no_previous_reason": None}
    d.update(over)
    return types.SimpleNamespace(**d)


def _sign_stable_164(c):
    """The fleet's stable release: a signed 1.64 record in release/ea_release.json + #ea (what ea-release committed)."""
    ex5 = c.tmp / "stable.ex5"
    ex5.write_bytes(STABLE_164)
    json.dump({"ea": {}}, open(c.hashes, "w"))
    with patch.object(c.ver, "mq5_property_version", lambda path=None: "1.64"):
        assert c.ver.record(_args(c, ex5)) == 0
    rec = json.load(open(c.rel))
    assert rec["version"] == "1.64" and rec["signature"]["sig_hex"] and rec["previous"] is None
    return rec


def _recapture_wipes_hashes_entry(c):
    """What the merge did: docs/RELEASE_HASHES.json#ea became an unsigned 1.65 capture (no ex5, no signature, no previous)."""
    doc = json.load(open(c.hashes))
    doc["ea"] = {"version": c.ver.mq5_property_version(), "mq5_sha256": c.ver.sha256_source(c.ver.MQ5),
                 "ex5_sha256": None, "ex5_note": "pending CI"}
    json.dump(doc, open(c.hashes, "w"))


def _release_new_version(c, **over):
    ex5 = c.tmp / "new.ex5"
    ex5.write_bytes(b"EX5-new-version")
    rc = c.ver.record(_args(c, ex5, **over))
    assert rc == 0
    c.ec._EXPECTED_CACHE.clear()
    return json.load(open(c.rel))


# ── record(): previous carried from the signed record the server ships ───────────────────────────────────
def test_record_carries_signed_previous_from_ea_release_json_after_hashes_entry_was_wiped(chain):
    c = chain
    stable = _sign_stable_164(c)
    _recapture_wipes_hashes_entry(c)
    assert c.ver.mq5_property_version() != "1.64"                     # the shipped MQ5 IS the new version
    rec = _release_new_version(c)
    assert rec["version"] == c.ver.mq5_property_version()
    assert rec["previous"]["version"] == "1.64" and rec["previous"]["ex5_sha256"] == stable["ex5_sha256"]
    assert rec["previous"]["signature"]["sig_hex"] == stable["signature"]["sig_hex"]   # the signed copy, not a pointer
    assert "previous_waiver" not in rec
    # the previous is signed INTO the new record (N104-6) and the server accepts current + previous
    import release_signing as rs
    assert rs.verify_hex(c.ec._canonical_payload(rec), rec["signature"]["sig_hex"], purpose="ea-release")
    assert c.ver._canonical_payload(rec) == c.ec._canonical_payload(rec)
    assert c.ec.accepted_ea_sha256s() == [rec["ex5_sha256"], stable["ex5_sha256"]]
    # docs/RELEASE_HASHES.json#ea mirrors the signed record
    assert json.load(open(c.hashes))["ea"]["previous"]["ex5_sha256"] == stable["ex5_sha256"]


def test_record_carries_previous_from_hashes_entry_previous_when_capture_carried_it(chain):
    c = chain
    stable = _sign_stable_164(c)
    os.remove(c.rel)                                                  # only #ea.previous knows the stable release
    doc = json.load(open(c.hashes))
    doc["ea"] = {"version": c.ver.mq5_property_version(), "mq5_sha256": c.ver.sha256_source(c.ver.MQ5),
                 "ex5_sha256": None, "previous": stable}
    json.dump(doc, open(c.hashes, "w"))
    rec = _release_new_version(c)
    assert rec["previous"]["ex5_sha256"] == stable["ex5_sha256"]


def test_record_fails_closed_when_version_changed_without_signed_prior(chain):
    c = chain
    json.dump({"ea": {"version": "1.64", "mq5_sha256": c.ver.sha256_source(c.ver.MQ5), "ex5_sha256": None}}, open(c.hashes, "w"))
    before = open(c.hashes).read()
    ex5 = c.tmp / "new.ex5"
    ex5.write_bytes(b"EX5-new-version")
    with pytest.raises(SystemExit) as e:
        c.ver.record(_args(c, ex5))
    msg = str(e.value)
    assert "NO SIGNED prior release record" in msg and "previous: null" in msg and "git checkout" in msg
    assert not os.path.exists(c.rel) and open(c.hashes).read() == before     # nothing written
    # the explicit waiver is the ONLY way to a null previous — and it is recorded in the release
    rec = _release_new_version(c, no_previous_reason="fresh fleet, no signed EA deployed")
    assert rec["previous"] is None and rec["previous_waiver"]["reason"] == "fresh fleet, no signed EA deployed"


def test_record_fails_when_the_prior_record_does_not_verify(chain):
    c = chain
    stable = _sign_stable_164(c)
    stable["ex5_sha256"] = "f" * 64                                   # tampered: signature no longer covers it
    json.dump(stable, open(c.rel, "w"))
    _recapture_wipes_hashes_entry(c)
    ex5 = c.tmp / "new.ex5"
    ex5.write_bytes(b"EX5-new-version")
    with pytest.raises(SystemExit) as e:
        c.ver.record(_args(c, ex5))
    assert "cannot be carried forward" in str(e.value) and "previous: null" in str(e.value)


def test_record_same_version_resign_keeps_previous_pointer(chain):
    c = chain
    stable = _sign_stable_164(c)
    _recapture_wipes_hashes_entry(c)
    first = _release_new_version(c)
    again = _release_new_version(c)                                   # ea-release re-run on the same MQ5
    assert again["version"] == first["version"] and again["previous"]["ex5_sha256"] == stable["ex5_sha256"]


def test_first_release_ever_has_no_previous_without_waiver(chain):
    c = chain
    json.dump({"ea": {}}, open(c.hashes, "w"))
    rec = _release_new_version(c)
    assert rec["previous"] is None and "previous_waiver" not in rec


# ── capture never discards the signed entry ──────────────────────────────────────────────────────────────
def test_capture_merge_preserves_signed_record():
    from capture_release_hashes import merge_ea_entry
    signed = {"version": "1.64", "mq5_sha256": "m" * 64, "ex5_sha256": "b" * 64, "compiled_by": "github-actions",
              "signature": {"key_id": "k", "sig_hex": "s"}, "previous": {"version": "1.63", "ex5_sha256": "c" * 64, "signature": {"sig_hex": "x"}}}
    cap_new = {"version": "1.65", "mq5_sha256": "n" * 64, "ex5_sha256": None, "ex5_note": "pending"}
    out = merge_ea_entry(dict(signed), dict(cap_new))
    assert out["version"] == "1.65" and out["ex5_sha256"] is None
    assert out["previous"]["ex5_sha256"] == "b" * 64 and out["previous"]["signature"]["sig_hex"] == "s"
    assert out["previous"]["previous"] == {"version": "1.63", "ex5_sha256": "c" * 64}             # one hop, pointer only
    same = {"version": "1.64", "mq5_sha256": "m" * 64, "ex5_sha256": None}
    assert merge_ea_entry(dict(signed), dict(same)) == signed                                      # same MQ5 → untouched
    carried = {"version": "1.65", "mq5_sha256": "n" * 64, "ex5_sha256": None, "previous": signed}
    assert merge_ea_entry(carried, {"version": "1.65", "mq5_sha256": "o" * 64, "ex5_sha256": None})["previous"] == signed
    assert merge_ea_entry({"version": "1.64", "mq5_sha256": "m" * 64}, dict(cap_new)) == cap_new  # unsigned → replaced
    assert merge_ea_entry(None, dict(cap_new)) == cap_new


def test_capture_script_uses_the_merge():
    src = open(os.path.join(ROOT, "scripts", "capture_release_hashes.py"), encoding="utf-8").read()
    assert 'out["ea"] = merge_ea_entry(existing, out["ea"])' in src


# ── fleet compatibility gate ─────────────────────────────────────────────────────────────────────────────
def test_fleet_compat_gate_blocks_a_record_that_drops_the_stable_ex5(chain):
    import check_ea_fleet_compat as gate
    c = chain
    stable = _sign_stable_164(c)
    _recapture_wipes_hashes_entry(c)
    good = _release_new_version(c)
    assert gate.check(good, stable["ex5_sha256"], "prev tag") == []
    assert gate.check(good, good["ex5_sha256"], "same") == []
    bad = dict(good, previous=None)                                    # the 14 Oct record shape
    fails = gate.check(bad, stable["ex5_sha256"], "prev tag")
    assert len(fails) == 1 and "NOT accepted" in fails[0] and "CLOSE_ONLY" in fails[0]
    unsigned_prev = dict(good, previous={"version": "1.64", "ex5_sha256": stable["ex5_sha256"]})   # grafted pointer only
    fails = gate.check(unsigned_prev, stable["ex5_sha256"], "prev tag")
    assert len(fails) == 1 and "could not be verified" in fails[0] and "no signature" in fails[0]
    assert gate.check(bad, None, "no tag") == []                       # nothing to compare against


def test_fleet_compat_gate_is_wired_into_release_ci_ea_release_and_update():
    rel = open(os.path.join(ROOT, ".github", "workflows", "release.yml"), encoding="utf-8").read()
    ci = open(os.path.join(ROOT, ".github", "workflows", "ci.yml"), encoding="utf-8").read()
    ear = open(os.path.join(ROOT, ".github", "workflows", "ea-release.yml"), encoding="utf-8").read()
    upd = open(os.path.join(ROOT, "deploy", "update.sh"), encoding="utf-8").read()
    lib = open(os.path.join(ROOT, "deploy", "lib.sh"), encoding="utf-8").read()
    assert "scripts/check_ea_fleet_compat.py" in rel and rel.index("check_ea_fleet_compat") < rel.index("  ci-gates:")   # tag-only job
    assert "scripts/check_ea_fleet_compat.py" in ci
    assert "check_ea_fleet_compat.py --stable-ref HEAD" in ear
    assert 'ea_fleet_compat_gate "${PREV}" || gate_refused' in upd and upd.index("ea_fleet_compat_gate") < upd.index("provision_images || rollback")
    assert 'git show "${prev}:release/ea_release.json"' in lib and "--stable-record" in lib


# ── deploy a new EA while terminals still run the previous signed EX5 ────────────────────────────────────
def _terminal(stable_sha: str, n: int) -> dict:
    """An attested-DEMO account whose terminal runs the stable (previous) EX5 — the 4 production terminals."""
    from broker_env import attestation_identity
    from auto_attest import AUTO_APPROVER, AUTO_VERIFIER
    now = datetime.now(timezone.utc).isoformat()
    acc = {"_id": ObjectId(), "user_id": "u1", "mode": "live", "label": f"terminal-{n}", "account_number": str(1083440 + n),
           "server": "RoboForex-Demo", "broker_server": "RoboForex-Demo", "expected_identity": {"broker_server": "RoboForex-Demo"},
           "account_type": "demo", "broker_environment": "DEMO", "broker_account_id_reported": str(1083440 + n),
           "last_heartbeat": now, "account_trade_mode": "demo", "account_trade_mode_reported_at": now,
           "ea_version": "1.64", "trading_enabled": True,
           "ea_identity": {"installation_id": f"inst_0123456789a{n}", "authoritative": True, "broker_server": "RoboForex-Demo", "trade_mode": "demo"},
           "ea_binary_sha256": stable_sha, "ea_binary_sha256_method": "installer_attested"}
    acc["environment_attestation"] = {"environment": "DEMO", "approved_by": AUTO_APPROVER, "at": now,
                                      "identity_hash": attestation_identity(acc), "proof": {"verifier": AUTO_VERIFIER}}
    return acc


def test_new_ea_release_keeps_terminals_on_previous_ex5_attested_demo_and_full(chain):
    import auto_attest as aa
    import broker_env
    import trading_authority as ta
    c = chain
    stable = _sign_stable_164(c)
    _recapture_wipes_hashes_entry(c)
    _release_new_version(c)                                           # server now ships the new EA; terminals still on 1.64
    accepted = c.ec.accepted_ea_sha256s()
    assert stable["ex5_sha256"] in accepted
    for n in range(4):
        acc = _terminal(stable["ex5_sha256"], n)
        d = aa.evaluate(acc, accepted_hashes=accepted)
        assert d["void"] is False and d["checks"]["ea_hash_signed"] is True, d
        assert broker_env.attested_environment(acc) == "DEMO"
        assert broker_env.ea_binary_accepted(acc) is True
        assert c.ec.live_gate(acc) is None
        assert _run(ta.infrastructure_domain(None, acc)) == {"level": "FULL", "reason": "terminal heartbeat fresh"}


def test_incident_shape_previous_null_voids_the_fleet(chain):
    """Negative control — the 14 Oct record (previous: null) is exactly what voided the 4 DEMO attestations."""
    import auto_attest as aa
    import broker_env
    c = chain
    stable = _sign_stable_164(c)
    _recapture_wipes_hashes_entry(c)
    os.remove(c.rel)                                                  # no signed prior anywhere → only a waiver gets here
    json.dump({"ea": json.load(open(c.hashes))["ea"]}, open(c.hashes, "w"))
    _release_new_version(c, no_previous_reason="simulate the 14 Oct record")
    accepted = c.ec.accepted_ea_sha256s()
    assert stable["ex5_sha256"] not in accepted
    acc = _terminal(stable["ex5_sha256"], 0)
    d = aa.evaluate(acc, accepted_hashes=accepted)
    assert d["void"] is True and d["checks"]["ea_hash_signed"] is False
    acc.pop("environment_attestation")
    assert broker_env.attested_environment(acc) == "LIVE" and c.ec.live_gate(acc)["code"] == "EA_BINARY_HASH_MISMATCH"
