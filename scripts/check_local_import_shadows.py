#!/usr/bin/env python3
"""Fail when a function-local `import` re-binds a name that the module already imports at top level.

Python makes such a name LOCAL for the WHOLE function body, so any use before the local import raises
UnboundLocalError — prod v1.60.22: Bot Config save returned 500 because update_config() re-imported
`audit_event` inside one branch (routes/bot_routes.py). ruff's F823 only catches the simplest shape;
this check covers every function in backend/ (tests excluded).

    python3 scripts/check_local_import_shadows.py            # exit 1 on any shadow
"""
from __future__ import annotations

import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCAN = os.path.join(ROOT, "backend")
SKIP_DIRS = {"tests", "node_modules", "__pycache__", ".venv", "venv", "models_store"}


def shadows_in(path: str) -> list[tuple[int, str, str]]:
    try:
        tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
    except SyntaxError as e:
        return [(e.lineno or 0, "<module>", f"syntax error: {e.msg}")]
    top = {a.asname or a.name.split(".")[0] for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
    out = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if isinstance(node, (ast.Import, ast.ImportFrom)) and node is not fn:
                for a in node.names:
                    name = a.asname or a.name.split(".")[0]
                    if name in top:
                        out.append((node.lineno, fn.name, name))
    return out


def main() -> int:
    bad = 0
    for dirpath, dirnames, filenames in os.walk(SCAN):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in sorted(filenames):
            if not f.endswith(".py"):
                continue
            p = os.path.join(dirpath, f)
            for lineno, fn, name in shadows_in(p):
                bad += 1
                print(f"{os.path.relpath(p, ROOT)}:{lineno}: local import of `{name}` inside {fn}() shadows the module-level import "
                      f"(UnboundLocalError on any use before this line) — delete the local import")
    print(f"local-import-shadow check: {'OK' if not bad else f'{bad} finding(s)'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
