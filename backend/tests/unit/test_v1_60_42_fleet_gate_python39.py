"""v1.60.42 — the EA fleet gate ran on the VPS with the host's Python 3.9.25: importing backend/ea_capabilities.py
raised TypeError (PEP 604 `X | None` evaluated at def time), previous_verifies() swallowed it and the valid 1.64
record was reported as 'does not verify' → v1.60.41 refused (false negative). Covered: the gate's import closure is
3.9-safe (static scan + a real python3.9 run when one is installed), failures are reported as 'cannot verify: …',
env pins load from backend/.env, deploy picks a capable interpreter and gate_refused restores the release truth."""
import ast
import json
import os
import shutil
import subprocess
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "backend"))
pytestmark = pytest.mark.unit

GATE = os.path.join(ROOT, "scripts", "check_ea_fleet_compat.py")


def _local_modules(path: str, seen: set) -> set:
    """Transitive closure of repo-local modules (backend/, scripts/) a file imports — including lazy imports."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module.split(".")[0]]
        for n in names:
            for d in ("backend", "scripts"):
                p = os.path.join(ROOT, d, n + ".py")
                if os.path.exists(p) and p not in seen:
                    seen.add(p)
                    _local_modules(p, seen)
    return seen


def _pep604_in_runtime_annotations(path: str) -> list:
    """Annotations evaluated at def time (no `from __future__ import annotations`) that use `X | Y` / PEP 585 generics."""
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    if any(isinstance(n, ast.ImportFrom) and n.module == "__future__" and any(a.name == "annotations" for a in n.names)
           for n in tree.body):
        return []
    bad = []
    for node in ast.walk(tree):
        anns = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            anns = [a.annotation for a in node.args.args + node.args.kwonlyargs if a.annotation] + ([node.returns] if node.returns else [])
        elif isinstance(node, ast.AnnAssign):
            anns = [node.annotation]
        for a in anns:
            for sub in ast.walk(a):
                if isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.BitOr):
                    bad.append(f"{os.path.relpath(path, ROOT)}:{sub.lineno} `{ast.get_source_segment(src, sub)}`")
                elif isinstance(sub, ast.Subscript) and isinstance(sub.value, ast.Name) and sub.value.id in ("list", "dict", "tuple", "set"):
                    bad.append(f"{os.path.relpath(path, ROOT)}:{sub.lineno} `{ast.get_source_segment(src, sub)}`")
    return bad


def test_gate_import_closure_is_python39_safe_statically():
    closure = _local_modules(GATE, {GATE})
    assert os.path.join(ROOT, "backend", "ea_capabilities.py") in closure and os.path.join(ROOT, "backend", "release_signing.py") in closure
    bad = [b for p in sorted(closure) for b in _pep604_in_runtime_annotations(p)]
    assert not bad, "runtime-evaluated PEP 604/585 annotations (add `from __future__ import annotations`):\n" + "\n".join(bad)


def _python39():
    for cand in ("python3.9", os.path.expanduser("~/.local/share/uv/python/cpython-3.9-linux-aarch64-gnu/bin/python3.9"),
                 os.path.expanduser("~/.local/share/uv/python/cpython-3.9-linux-x86_64-gnu/bin/python3.9")):
        p = shutil.which(cand) or (cand if os.path.exists(cand) else None)
        if p:
            return p
    return None


@pytest.mark.skipif(_python39() is None, reason="python3.9 not installed here (CI static-analysis runs the host-compat gate under 3.9)")
def test_gate_verifies_a_signed_previous_under_real_python39(tmp_path):
    """The exact VPS scenario: gate under 3.9, record whose stable EX5 is only accepted via a signed `previous`."""
    py39 = _python39()
    has_crypto = subprocess.run([py39, "-c", "import cryptography"], capture_output=True).returncode == 0
    probe = subprocess.run([py39, "-c", "import sys; sys.path.insert(0, %r); import ea_capabilities, release_signing; print('ok')"
                            % os.path.join(ROOT, "backend")], capture_output=True, text=True)
    assert probe.returncode == 0 and "ok" in probe.stdout, probe.stderr[-800:]
    if not has_crypto:
        pytest.skip("python3.9 here has no cryptography wheel — import check done, signature run needs it")
    # build a signed 1.64 → 1.65 chain with a local key, then run the gate under 3.9 with those pins
    import verify_ea_release as ver
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization as s
    import base64
    k = Ed25519PrivateKey.generate()
    env = dict(os.environ, RELEASE_SIGNER="local", APP_ENV="preview", RELEASE_SIGNER_DEFERRED="false", GITHUB_ACTIONS="true", GITHUB_SHA="c" * 40,
               ED25519_SIGNING_KEY_B64=base64.b64encode(k.private_bytes(s.Encoding.Raw, s.PrivateFormat.Raw, s.NoEncryption())).decode(),
               RELEASE_PUBLIC_KEY_B64=base64.b64encode(k.public_key().public_bytes(s.Encoding.Raw, s.PublicFormat.Raw)).decode())
    for kk in ("EA_RELEASE_SHA256", "EA_RELEASE_SHA256_PREVIOUS", "RELEASE_SIGNER_KEY_ID"):
        env.pop(kk, None)
    hashes, rel, log = tmp_path / "RELEASE_HASHES.json", tmp_path / "release" / "ea_release.json", tmp_path / "compile.log"
    log.write_bytes(("\ufeff" + "MetaEditor 5.00 build 4620\nResult: 0 errors, 0 warnings\n").encode("utf-16"))
    saved = {kk: os.environ.get(kk) for kk in env}
    os.environ.update(env)
    try:
        ver.HASHES, ver.EA_RELEASE, ver.RC_LOCK = str(hashes), str(rel), str(tmp_path / "rc_lock.json")
        json.dump({"ea": {}}, open(hashes, "w"))
        (tmp_path / "stable.ex5").write_bytes(b"EX5-1.64")
        args = dict(ex5=str(tmp_path / "stable.ex5"), compile_log=str(log), metaeditor_version=None, windows_build="10.0.26100", mt5_build=None,
                    sign=True, source_commit="a" * 40, compiled_by="github-actions", no_previous_reason=None)
        from unittest.mock import patch
        with patch.object(ver, "mq5_property_version", lambda path=None: "1.64"):
            assert ver.record(types.SimpleNamespace(**args)) == 0
        stable_sha = json.load(open(rel))["ex5_sha256"]
        doc = json.load(open(hashes))
        doc["ea"] = {"version": ver.mq5_property_version(), "mq5_sha256": ver.sha256_source(ver.MQ5), "ex5_sha256": None}
        json.dump(doc, open(hashes, "w"))
        (tmp_path / "new.ex5").write_bytes(b"EX5-1.65")
        assert ver.record(types.SimpleNamespace(**dict(args, ex5=str(tmp_path / "new.ex5")))) == 0
    finally:
        for kk, v in saved.items():
            if v is None:
                os.environ.pop(kk, None)
            else:
                os.environ[kk] = v
    env_file = tmp_path / "backend.env"                       # the gate must load the RELEASE_* pins itself
    env_file.write_text(f"RELEASE_PUBLIC_KEY_B64={env['RELEASE_PUBLIC_KEY_B64']}\n")
    run_env = {kk: v for kk, v in os.environ.items() if not kk.startswith("RELEASE_") and kk not in ("EA_RELEASE_SHA256", "EA_RELEASE_SHA256_PREVIOUS")}
    run_env["STOIC_BACKEND_ENV_FILE"] = str(env_file)
    r = subprocess.run([py39, GATE, "--record", str(rel), "--stable-sha", stable_sha], capture_output=True, text=True, env=run_env, cwd=ROOT)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "accepts the fleet stable EX5" in r.stdout and "(signed previous)" in r.stdout
    assert "cannot verify" not in r.stdout and "does not verify" not in r.stdout


def test_previous_verifies_reports_cannot_verify_instead_of_false(monkeypatch):
    import check_ea_fleet_compat as gate
    import builtins
    real_import = builtins.__import__

    def _boom(name, *a, **kw):
        if name == "ea_capabilities":
            raise TypeError("unsupported operand type(s) for |: 'type' and 'NoneType'")
        return real_import(name, *a, **kw)
    monkeypatch.setattr(builtins, "__import__", _boom)
    rec = {"ex5_sha256": "a" * 64, "previous": {"version": "1.64", "ex5_sha256": "b" * 64, "compiled_by": "github-actions",
                                                 "signature": {"key_id": "k", "sig_hex": "00"}}}
    ok, why = gate.previous_verifies(rec)
    assert ok is False and why.startswith("cannot verify: TypeError: unsupported operand") and "python" in why
    fails = gate.check(rec, "b" * 64, "prev")
    assert len(fails) == 1 and "cannot verify" in fails[0] and "GATE problem, not a record problem" in fails[0] and "docker compose run" in fails[0]
    assert gate.previous_verifies({"ex5_sha256": "a" * 64}) == (False, "no previous record")


def test_gate_loads_backend_env_pins_before_checking():
    src = open(GATE, encoding="utf-8").read()
    assert src.index("    load_backend_env_pins()") < src.index("stable, desc = stable_from_args(args)")


def test_deploy_runs_the_gate_with_a_capable_interpreter_and_restores_release_truth():
    lib = open(os.path.join(ROOT, "deploy", "lib.sh"), encoding="utf-8").read()
    upd = open(os.path.join(ROOT, "deploy", "update.sh"), encoding="utf-8").read()
    assert '"$(gate_python)" scripts/check_ea_fleet_compat.py --stable-record' in lib
    assert "import sys, cryptography; sys.exit(0 if sys.version_info >= (3, 9) else 1)" in lib
    assert "restore_release_truth()" in lib and "rm -f release/attestation.current.json release/attestation.current.bundle deploy/state/release/attestation.current.json" in lib
    gr = upd[upd.index("gate_refused() {"):upd.index("rollback() {")]
    assert 'restore_release_truth "${PREV}"' in gr and gr.index('git checkout --detach "${PREV}"') < gr.index("restore_release_truth")
    assert "ea_capabilities" in open(os.path.join(ROOT, "scripts", "check_host_python_compat.py"), encoding="utf-8").read()


def test_gate_python_picks_an_interpreter_with_cryptography(tmp_path):
    """Pure-bash check of gate_python: a bare python3.11 stub (no cryptography) is skipped for the capable python3."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "python3.11").write_text("#!/bin/bash\nexit 1\n")          # no cryptography → import fails
    (fake_bin / "python3").write_text("#!/bin/bash\nexit 0\n")
    for f in fake_bin.iterdir():
        f.chmod(0o755)
    lib_src = open(os.path.join(ROOT, "deploy", "lib.sh"), encoding="utf-8").read()
    fn = lib_src[lib_src.index("gate_python() {"):]
    fn = fn[:fn.index("\n}\n") + 3]
    r = subprocess.run(["/bin/bash", "-c", fn + "\ngate_python"], capture_output=True, text=True, env={"PATH": str(fake_bin)})
    assert r.returncode == 0 and r.stdout.strip() == str(fake_bin / "python3"), r.stdout + r.stderr
