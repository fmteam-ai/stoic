"""CI gate: MQL5 EA structural sanity + version consistency.

A real MetaEditor compile needs a Windows MT5 install; this script catches
the failure classes that source edits actually introduce (unbalanced
braces/parens/brackets after stripping strings & comments, and version
drift between the EA and the backend/frontend constants).
"""
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EA = os.path.join(REPO, "backend", "static", "EmergentTradingBridge.mq5")


def strip_mql(src: str) -> str:
    out, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        if c == '"':
            i += 1
            while i < n and src[i] != '"':
                i += 2 if src[i] == "\\" else 1
            i += 1
        elif c == "'":
            i += 1
            while i < n and src[i] != "'":
                i += 2 if src[i] == "\\" else 1
            i += 1
        elif src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif src.startswith("/*", i):
            j = src.find("*/", i)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def main() -> int:
    src = open(EA).read()
    stripped = strip_mql(src)
    failures = []
    for a, b in (("{", "}"), ("(", ")"), ("[", "]")):
        ca, cb = stripped.count(a), stripped.count(b)
        print(f"{a}{b}: {ca}/{cb}")
        if ca != cb:
            failures.append(f"unbalanced {a}{b}: {ca} vs {cb}")

    m = re.search(r'#property version\s+"([\d.]+)"', src)
    d = re.search(r'#define EA_CLIENT_VERSION "([\d.]+)"', src)
    if not m or not d or m.group(1) != d.group(1):
        failures.append("EA #property version != EA_CLIENT_VERSION")
    v = m.group(1) if m else "?"
    print("EA version:", v)

    consistency = [
        (os.path.join(REPO, "backend", "routes", "bot_routes.py"),
         f'LATEST_EA = "{v}"'),
        (os.path.join(REPO, "backend", "routes", "diagnostic_routes.py"),
         f'LATEST_EA = "{v}"'),
        (os.path.join(REPO, "backend", "routes", "setup_routes.py"),
         f'"ea_latest_version": "{v}"'),
        (os.path.join(REPO, "frontend", "src", "pages", "Accounts.jsx"),
         f'LATEST_EA_VERSION = "{v}"'),
        (os.path.join(REPO, "frontend", "src", "components",
                      "EaVersionStrip.jsx"),
         f'LATEST_EA_VERSION = "{v}"'),
    ]
    for path, needle in consistency:
        if needle not in open(path).read():
            failures.append(f"version drift: {needle!r} missing in {path}")

    if failures:
        print("FAIL:")
        for f in failures:
            print(" -", f)
        return 1
    print("EA structural + version checks OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
