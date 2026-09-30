"""r26 P1-01 (code half) — release evidence is produced from ONE staged tree and
installation/packaging refuse a broken provenance chain."""
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
SCRIPTS = os.path.join(REPO, "scripts")


def _tree(tmp_path, n_tests=1):
    (tmp_path / "backend" / "tests").mkdir(parents=True)
    (tmp_path / "e2e" / "tests").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    (tmp_path / "backend" / "tests" / "test_a.py").write_text("".join(f"def test_{i}():\n    pass\n" for i in range(n_tests)))
    (tmp_path / "docs" / "TEST_MANIFEST.md").write_text("")
    return tmp_path


def _gen(root, *args):
    return subprocess.run([sys.executable, os.path.join(SCRIPTS, "generate_test_manifest.py"), "--root", str(root), *args],
                          capture_output=True, text=True)


def test_manifest_generates_and_checks_inside_a_staged_tree(tmp_path):
    root = _tree(tmp_path, 3)
    assert _gen(root, "--check").returncode == 1                       # empty manifest is stale
    assert _gen(root).returncode == 0
    body = (root / "docs" / "TEST_MANIFEST.md").read_text()
    assert "`backend/tests/test_a.py`" in body and "**Total: 3 tests across 1 files.**" in body
    assert _gen(root, "--check").returncode == 0
    (root / "backend" / "tests" / "test_b.py").write_text("def test_x():\n    pass\n")   # a late test file
    r = _gen(root, "--check")
    assert r.returncode == 1 and "stale" in r.stdout


def test_freeze_refuses_a_stale_test_manifest(tmp_path):
    root = _tree(tmp_path, 2)
    (root / "release").mkdir()
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "freeze_rc_lock.py"), "--root", str(root),
                        "--out", str(root / "release" / "rc_lock.json")], capture_output=True, text=True)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "TEST_MANIFEST.md is stale" in r.stdout and "refusing to bind" in r.stdout
    assert not (root / "release" / "rc_lock.json").exists()             # nothing frozen


def test_release_workflow_freezes_from_the_staged_tree():
    wf = open(os.path.join(REPO, ".github", "workflows", "release.yml")).read()
    stage = wf.index("generate_test_manifest.py --root /tmp/pkg --check")
    freeze = wf.index("freeze_rc_lock.py --root /tmp/pkg --commit")
    check = wf.index("release_consistency_check.py --root /tmp/pkg --strict")
    pack = wf.index('tar -czf "stoic-${REF}.tar.gz" -C /tmp/pkg .')
    assert stage < freeze < check < pack                                # evidence → lock → gate → archive
    assert "cmp -s docs/TEST_MANIFEST.md /tmp/pkg/docs/TEST_MANIFEST.md" in wf


def test_installer_and_updater_refuse_inconsistent_provenance():
    lib = open(os.path.join(REPO, "deploy", "lib.sh")).read()
    assert "verify_release_provenance()" in lib and "release_consistency_check.py ${strict}" in lib
    assert 'attestation_required && strict="--strict"' in lib
    inst = open(os.path.join(REPO, "deploy", "install.sh")).read()
    assert inst.index("verify_release_provenance || exit 1") < inst.index("provision_images || exit 1")
    upd = open(os.path.join(REPO, "deploy", "update.sh")).read()
    assert upd.index("verify_release_provenance || rollback") < upd.index("provision_images || rollback")


def test_consistency_check_treats_digest_mismatch_as_failure_even_in_snapshot_mode():
    sys.path.insert(0, SCRIPTS)
    import release_consistency_check as rcc
    facts = {"lock.test_manifest_sha256": "a" * 64, "actual.test_manifest_sha256": "b" * 64,
             "lock.model_manifest_sha256": None, "actual.model_manifest_sha256": None,
             "lock.git_commit": "c1", "lock.source_sha": "c1", "expected.commit": None,
             "build_sha": "c1", "model_manifest.code_commit": "c1", "release_summary.source_commit": "c1",
             "deployed.build": None, "expected.images.backend": None, "expected.images.frontend": None,
             "lock.images.backend": None, "lock.images.frontend": None, "lock.authoritative": False}
    mism, _ = rcc.compare(facts, strict=False)
    assert any(m.startswith("test_manifest_sha256") for m in mism)
