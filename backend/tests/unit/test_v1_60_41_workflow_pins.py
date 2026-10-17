"""v1.60.41 — workflow pip pins must be installable on the interpreter of their job (CI static-analysis broke:
`python3.9 -m pip install requests==2.34.2` → requests ≥2.33 needs Python ≥3.10)."""
import glob
import os
import re

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
pytestmark = pytest.mark.unit

# minimum Python per pinned version (from PyPI Requires-Python)
MIN_PY = {("requests", "2.34.2"): (3, 10), ("requests", "2.32.5"): (3, 9), ("cryptography", "50.0.0"): (3, 9),
          ("pymongo", "4.18.2"): (3, 9)}
PIN = re.compile(r"\b(cryptography|requests|pymongo)==(\d+\.\d+\.\d+)")
PYVER = re.compile(r'python-version:\s*"(\d+)\.(\d+)"')


def _job_python(job: dict) -> list:
    """Interpreter in effect for each step: setup-python's version (or None = runner default) at that point."""
    cur, out = None, []
    for st in job.get("steps") or []:
        if "setup-python" in str(st.get("uses", "")):
            m = PYVER.search(f'python-version: "{(st.get("with") or {}).get("python-version", "")}"')
            cur = (int(m.group(1)), int(m.group(2))) if m else None
        out.append((cur, st))
    return out


def test_every_pinned_install_fits_the_interpreter_of_its_step():
    problems = []
    for wf in glob.glob(os.path.join(ROOT, ".github", "workflows", "*.yml")):
        doc = yaml.safe_load(open(wf, encoding="utf-8"))
        for jname, job in (doc.get("jobs") or {}).items():
            for py, st in _job_python(job):
                run = str(st.get("run") or "")
                explicit = re.search(r"python(\d)\.(\d+) -m pip", run)
                eff = (int(explicit.group(1)), int(explicit.group(2))) if explicit else py
                for pkg, ver in PIN.findall(run):
                    need = MIN_PY.get((pkg, ver))
                    assert need is not None, f"{os.path.basename(wf)}:{jname} pins {pkg}=={ver} — add its Requires-Python to MIN_PY"
                    if eff is None:
                        problems.append(f"{os.path.basename(wf)}:{jname} installs {pkg}=={ver} on the runner-default python — add setup-python")
                    elif eff < need:
                        problems.append(f"{os.path.basename(wf)}:{jname} installs {pkg}=={ver} (needs py{need}) on python {eff}")
    assert not problems, "\n".join(problems)


def test_host_compat_gate_keeps_the_py39_requests_pin():
    ci = open(os.path.join(ROOT, ".github", "workflows", "ci.yml"), encoding="utf-8").read()
    assert "python3.9 -m pip install -q cryptography==50.0.0 requests==2.32.5" in ci
