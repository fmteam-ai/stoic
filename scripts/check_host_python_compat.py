#!/usr/bin/env python3
"""M121-4 — host-side Python compatibility gate. Operator hosts run the SYSTEM python3 (AlmaLinux 9 = 3.9,
AlmaLinux 8 = 3.6 for `python3 -c` snippets). `py_compile` does not catch `def f(x: str | None)` — annotations
are evaluated at import time — so this gate actually IMPORTS every script that deploy/*.sh calls, plus the backend
modules those scripts import, under the interpreter it runs with (CI: actions/setup-python 3.9).

  python3.9 scripts/check_host_python_compat.py            → exit 1 on any import failure
"""
from __future__ import annotations

import ast
import importlib
import os
import re
import runpy
import sys
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIN_SUPPORTED = (3, 9)
# backend modules a host script may import whose own imports are pure (no DB, no app bootstrap)
BACKEND_IMPORT_ALLOWLIST = {"release_signing", "model_manifest", "base"}


def host_scripts() -> list[str]:
    names: set[str] = set()
    for d in ("deploy", os.path.join("deploy", "signer"), os.path.join("deploy", "migrator")):
        p = os.path.join(ROOT, d)
        if not os.path.isdir(p):
            continue
        for f in os.listdir(p):
            if f.endswith(".sh"):
                names.update(re.findall(r"scripts/([A-Za-z_]+\.py)", open(os.path.join(p, f), errors="replace").read()))
    return sorted(n for n in names if os.path.exists(os.path.join(ROOT, "scripts", n)))


def imported_modules(path: str) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(open(path).read())):
        if isinstance(node, ast.Import):
            out.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module.split(".")[0])
    return out


def main() -> int:
    ver = sys.version_info[:2]
    print(f"host python compat gate under Python {ver[0]}.{ver[1]} (minimum supported {MIN_SUPPORTED[0]}.{MIN_SUPPORTED[1]})")
    if ver > MIN_SUPPORTED:
        print(f"   note: run this under Python {MIN_SUPPORTED[0]}.{MIN_SUPPORTED[1]} to be meaningful (newer interpreters accept more syntax)")
    scripts = host_scripts()
    if not scripts:
        print("!! no host scripts discovered under deploy/"); return 1
    sys.path.insert(0, os.path.join(ROOT, "backend")); sys.path.insert(0, os.path.join(ROOT, "scripts"))
    failures, backend_mods = [], set()
    for name in scripts:
        path = os.path.join(ROOT, "scripts", name)
        try:
            runpy.run_path(path, run_name="__hostcheck__")
            print(f"   ok    scripts/{name}")
        except SystemExit as e:   # a script that runs at import and exits cleanly is fine; a non-zero exit is not
            if e.code not in (0, None):
                failures.append((name, f"SystemExit({e.code}) at import")); print(f"   FAIL  scripts/{name}: SystemExit({e.code})")
            else:
                print(f"   ok    scripts/{name}")
        except Exception:  # noqa: BLE001
            failures.append((name, traceback.format_exc().strip().splitlines()[-1])); print(f"   FAIL  scripts/{name}: {failures[-1][1]}")
        backend_mods.update(m for m in imported_modules(path) if m in BACKEND_IMPORT_ALLOWLIST
                            and os.path.exists(os.path.join(ROOT, "backend", m + ".py")))
    for m in sorted(backend_mods):   # lazy `from release_signing import …` inside functions runs on the host too
        try:
            importlib.import_module(m)
            print(f"   ok    backend/{m}.py (imported by a host script)")
        except ModuleNotFoundError as e:
            if e.name in BACKEND_IMPORT_ALLOWLIST:
                failures.append((m, str(e))); print(f"   FAIL  backend/{m}.py: {e}")
            else:
                print(f"   skip  backend/{m}.py: third-party module missing here ({e.name}) — install it to check")
        except Exception:  # noqa: BLE001
            failures.append((m, traceback.format_exc().strip().splitlines()[-1])); print(f"   FAIL  backend/{m}.py: {failures[-1][1]}")
    if failures:
        print(f"!! {len(failures)} host-side Python file(s) do not import under {ver[0]}.{ver[1]}: " + ", ".join(n for n, _ in failures))
        print("   fix: `from __future__ import annotations` (PEP 604/585 annotations) or avoid syntax newer than the host python")
        return 1
    print(f"OK: {len(scripts)} host script(s) + {len(backend_mods)} backend module(s) import under Python {ver[0]}.{ver[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
