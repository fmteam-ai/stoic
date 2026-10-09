#!/usr/bin/env python3
"""M115-1 — every `python3 -c '...'` snippet embedded in deploy/*.sh must parse under Python 3.6 syntax
(AlmaLinux 8 system python). Backslashes inside f-string braces, walrus, match, etc. are rejected.
    python3 scripts/check_deploy_python_snippets.py            → exit 1 on the first offending snippet
"""
import ast
import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNIPPET_RE = re.compile(r"python3?\s+-c\s+'((?:[^'\\]|\\.)*)'", re.S)


def snippets(path):
    text = open(path, encoding="utf-8").read()
    for m in SNIPPET_RE.finditer(text):
        line = text.count("\n", 0, m.start()) + 1
        yield line, m.group(1)


def check(path):
    bad = []
    for line, code in snippets(path):
        try:
            ast.parse(code, filename=f"{path}:{line}", feature_version=(3, 6))
        except SyntaxError as e:
            bad.append((line, str(e)))
    return bad


def main():
    files = sorted(glob.glob(os.path.join(ROOT, "deploy", "*.sh")))
    total = 0
    failures = []
    for f in files:
        n = sum(1 for _ in snippets(f))
        total += n
        for line, err in check(f):
            failures.append(f"{os.path.relpath(f, ROOT)}:{line}: {err}")
    for x in failures:
        print("FAIL", x)
    print(f"checked {total} python3 -c snippet(s) in {len(files)} deploy script(s) against Python 3.6 syntax")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
