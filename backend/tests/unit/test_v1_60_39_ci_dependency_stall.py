"""v1.60.39 — Release #61 (v1.60.38) ci-gates/backend-unit hung 52 min in the dependency step: the
emergentintegrations download from the Emergent CDN stalled without erroring, so the 5× retry loop never fired.
Every pip download of that wheel must be wall-clock capped (`timeout 120`) with pip's own socket timeout, and the
backend-unit job must carry a job-level timeout so a stall can never consume the 6 h default."""
import os
import re

import pytest

pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


@pytest.mark.parametrize("path", [
    (".github", "workflows", "ci.yml"),
    (".github", "workflows", "release.yml"),
    ("Dockerfile.backend",),
    ("scripts", "verify_release.sh"),
])
def test_every_emergentintegrations_download_is_wall_clock_capped(path):
    src = _read(*path)
    lines = [ln for ln in src.splitlines() if "pip download" in ln and "emergentintegrations==" in ln]
    assert lines, f"{'/'.join(path)}: expected at least one emergentintegrations pip download"
    for ln in lines:
        assert re.search(r"\btimeout \d+ (python -m )?pip download", ln), ln
        assert "--timeout 20" in ln and "--retries 1" in ln, ln


def test_backend_unit_job_has_job_timeout():
    ci = _read(".github", "workflows", "ci.yml")
    m = re.search(r"\n  backend-unit:\n(.*?)\n    steps:", ci, re.DOTALL)
    assert m and re.search(r"timeout-minutes: (\d+)", m.group(1)), "backend-unit needs timeout-minutes"
    assert int(re.search(r"timeout-minutes: (\d+)", m.group(1)).group(1)) <= 45
