"""v1.60.34 — release_gate image-digest compare.
Production: STOIC_IMAGE_DIGEST is the bare `sha256:…` while rc_lock.images.backend is the full
`ghcr.io/org/stoic-backend@sha256:…` reference → "running image digest differs" on the SAME image. Compare the digest
part only; pin the registry digest (RepoDigests) in build mode; update.sh requires evaluate() to pass in production.
"""
import os

import pytest

pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
DIG = "sha256:8d7b" + "0" * 60
LOCK = {"authoritative": True, "git_commit": "35164315abcdef",
        "images": {"backend": f"ghcr.io/fmteam-ai/stoic-backend@{DIG}", "frontend": "ghcr.io/fmteam-ai/stoic-frontend@sha256:" + "f" * 64}}


def _src(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def test_bare_digest_matches_full_repo_at_digest_reference():
    import release_gate as rg
    assert rg._bare_digest(f"ghcr.io/fmteam-ai/stoic-backend@{DIG}") == DIG == rg._bare_digest(DIG) == rg._bare_digest(f" {DIG}\n")
    assert rg._bare_digest(None) == "" and rg._bare_digest("") == ""
    # bare running digest vs full locked reference → same image → gate passes
    res = rg.evaluate(lock=LOCK, env={"STOIC_IMAGE_DIGEST": DIG}, ea_signed=True)
    assert res["ok"], res["failures"]
    # full reference on both sides, and full running vs bare locked, also pass
    assert rg.evaluate(lock=LOCK, env={"STOIC_IMAGE_DIGEST": LOCK["images"]["backend"]}, ea_signed=True)["ok"]
    bare_lock = {**LOCK, "images": {**LOCK["images"], "backend": DIG}}
    assert rg.evaluate(lock=bare_lock, env={"STOIC_IMAGE_DIGEST": LOCK["images"]["backend"]}, ea_signed=True)["ok"]


def test_a_genuinely_different_digest_still_fails():
    import release_gate as rg
    other = "sha256:" + "a" * 64
    res = rg.evaluate(lock=LOCK, env={"STOIC_IMAGE_DIGEST": other}, ea_signed=True)
    assert not res["ok"] and res["failures"] == ["running image digest differs from the locked backend digest"]
    res = rg.evaluate(lock=LOCK, env={"STOIC_IMAGE_DIGEST": f"ghcr.io/fmteam-ai/stoic-backend@{other}"}, ea_signed=True)
    assert not res["ok"] and "running image digest differs" in res["failures"][0]
    assert "STOIC_IMAGE_DIGEST not injected" in rg.evaluate(lock=LOCK, env={}, ea_signed=True)["failures"][0]


def test_build_mode_pins_the_registry_digest_not_the_image_id():
    lib = _src("deploy", "lib.sh")
    i = lib.index("build_with_provenance() {")
    body = lib[i:lib.index("\n}\n", i)]
    assert "{{index .RepoDigests 0}}" in body and 'STOIC_IMAGE_DIGEST="${repo_digest#*@}"' in body
    assert body.index(".RepoDigests") < body.index("'{{.Id}}'")          # RepoDigests first, .Id only as fallback
    # the registry path already pinned the bare registry digest
    assert 'set_kv .env STOIC_IMAGE_DIGEST "${be#*@}"' in lib


def test_update_sh_requires_the_full_release_gate_in_production():
    upd = _src("deploy", "update.sh")
    i = upd.index("from release_gate import evaluate")
    block = upd[upd.rindex("\n", 0, i - 200):i + 400]
    assert 'if [ "$(app_env)" = "production" ]; then' in block
    assert 'sys.exit(0 if g["ok"] else 1)' in upd and upd.index('sys.exit(0 if g["ok"] else 1)') < upd.index("informational")
    # the production branch rolls back on failure; the non-production branch only reports
    prod_line = next(ln for ln in upd.splitlines() if 'sys.exit(0 if g["ok"] else 1)' in ln)
    assert prod_line.rstrip().endswith("|| rollback")
    info_line = next(ln for ln in upd.splitlines() if "informational" in ln and "evaluate" in ln)
    assert info_line.rstrip().endswith("|| true")
