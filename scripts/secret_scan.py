#!/usr/bin/env python3
"""Repository credential scanner (audit round 10 P0-02 / P2-04).

Scans the WHOLE working tree (source, top-level tests, docs, workflows, scripts,
reports, e2e) — and optionally the full Git history — for credential
assignment patterns, known retired passwords and high-entropy literals next
to credential-looking names. Exit 1 on any finding. Allowlist: obvious
placeholders, documented dummy fixtures, and paths listed in
scripts/secret_scan_allowlist.txt.

    python scripts/secret_scan.py [--history] [--root PATH] [--json out.json]
"""
import argparse
import json
import math
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".pytest_cache", "dist", "build", ".venv", "venv",
             "playwright-report", "test-results", ".emergent"}
TEXT_EXT = {".py", ".js", ".jsx", ".ts", ".tsx", ".sh", ".yml", ".yaml", ".json", ".md", ".ps1", ".env",
            ".txt", ".toml", ".ini", ".cfg", ".mq5", ".mqh", ".html", ".css", ".Dockerfile", ""}
RETIRED_LITERALS = ("adm" + "in123",)          # assembled so the literal never appears in source
UPPER_PLACEHOLDER = re.compile(r"^[A-Z0-9_]{6,}$")                    # PASTE_YOUR_TOKEN_HERE (case-sensitive)
PLACEHOLDERS = re.compile(r"^(<[^>]+>|\$\{[^}]+\}|\$[A-Z_]+|x+|\*+|\.{3}|changeme|change-me|your[-_ ].*|"
                          r"redacted.*|placeholder|example|secret|password|dummy.*|test.*|fake.*|none|null|"
                          r"true|false|\d{1,3}|[a-z_]+\([^)]*\)|os\.environ.*|process\.env.*)$", re.I)
CRED_NAME = r"(?:pass(?:word|wd)?|passwd|pwd|secret|token|api[_-]?key|private[_-]?key|credential)s?"
ASSIGN = re.compile(rf"(?i)\b[A-Z0-9_.\-]*{CRED_NAME}[A-Z0-9_]*\b\s*[:=]\s*['\"]([^'\"\n]{{6,}})['\"]")
JSON_KEY = re.compile(rf"(?i)['\"][A-Z0-9_\-]*{CRED_NAME}['\"]\s*:\s*['\"]([^'\"\n]{{6,}})['\"]")
LOGIN_BODY = re.compile(r"(?i)['\"]?email['\"]?\s*[:=]\s*['\"]([^'\"]+@[^'\"]+)['\"]\s*,\s*['\"]?password['\"]?\s*[:=]\s*['\"]([^'\"\n]{6,})['\"]")
# fixture-looking values shared by many suites are ALLOWED only when they never authenticate anywhere
ALLOWED_FIXTURE_VALUES = {"Kd5#Zt9mW2xVpR7c", "Gy6#Vb3kM9zRnD2s", "dummy-secret", "dummy-site", "testkey-testkey-testkey",
                          "ref:kms/release-signer", "vault-master-material", "leaked-private-key", "some-other-key",
                          "definitely-wrong-password-1!", "secret-token-value"}


def _entropy(s: str) -> float:
    if not s:
        return 0.0
    probs = [s.count(c) / len(s) for c in set(s)]
    return -sum(p * math.log2(p) for p in probs)


def _retired_hashes() -> set:
    p = os.path.join(ROOT, "scripts", "retired_credentials.sha256")
    if not os.path.exists(p):
        return set()
    return {l.split()[0] for l in open(p) if l.strip() and not l.startswith("#")}


RETIRED_HASHES = _retired_hashes()
INFO_KINDS = {"retired-literal", "retired-rotated"}


def _rotated(value: str) -> bool:
    import hashlib
    return hashlib.sha256(value.strip().encode()).hexdigest() in RETIRED_HASHES


def _allowlist():
    p = os.path.join(ROOT, "scripts", "secret_scan_allowlist.txt")
    if not os.path.exists(p):
        return []
    return [l.strip() for l in open(p) if l.strip() and not l.startswith("#")]


def _suspicious(value: str) -> str | None:
    v = value.strip()
    if _rotated(v):
        return "retired-rotated"
    if v in ALLOWED_FIXTURE_VALUES or PLACEHOLDERS.match(v) or UPPER_PLACEHOLDER.match(v):
        return None
    if any(r in v for r in RETIRED_LITERALS):
        return "retired-literal"
    # templated / shell variable — only when what is left after removing the placeholders is
    # too short to be a secret (audit P3: a real key that merely CONTAINS "{x}" must not hide)
    if re.fullmatch(r"\$\{?[A-Z_][A-Z0-9_]*\}?", v):
        return None
    if re.search(r"\{\{|\{[a-zA-Z_]+\}|%s|\$\(", v):
        residue = re.sub(r"\{\{.*?\}\}|\{[a-zA-Z_]+\}|%s|\$\([^)]*\)|\$\{?[A-Z_][A-Z0-9_]*\}?", "", v)
        if len(residue) < 16:
            return None
    if re.search(r"\s", v) or re.fullmatch(r"[a-z][a-z\-]*", v):      # prose / kebab-case identifiers
        return None
    if re.match(r"^(text|bg|border|ring|from|to|via)-\[?#?[0-9A-Fa-f]{3,8}\]?$", v) or v.startswith(("http://", "https://")):
        return None                                                    # tailwind colour / URL
    if re.match(r"^(run|req|dec|authsnap|int|pgm|agt|sess)_[0-9a-f]{8,}$", v):
        return None                                                    # platform run/record identifiers
    if re.match(r"^[a-z]{2,5}:[a-z0-9]+(,[a-z]{2,5}:[a-z0-9]+)*$", v):
        return None                                                    # documented "region:tok" example shape
    if len(v) >= 12 and _entropy(v) >= 3.0 and re.search(r"[A-Za-z]", v) and re.search(r"[0-9]", v) \
            and re.search(r"[0-9#!@$%^&*_\-]", v):
        return "high-entropy-credential-literal"
    return None


TEST_PATH_PREFIXES = ("backend/tests/", "e2e/", "frontend/src/__tests__/", "frontend/e2e/", "test_reports/")
STABLE_ACCOUNT_HINT = re.compile(r"(?i)admin@|ops@|owner@|support@|@stoicaibot\.com|@trading\.bot")
STABLE_PAIR_PROSE = re.compile(
    r"(?i)((?:admin|ops|owner|support)@[\w.-]+\.\w+|[\w.+-]+@(?:stoicaibot\.com|trading\.bot))"
    r"\s*(?:/|:|\||,|->|—|-)\s*([^\s'\"`<>()]{8,})")


def _is_test_path(path: str) -> bool:
    return path.startswith(TEST_PATH_PREFIXES)


def scan_text(text: str, path: str, findings: list) -> None:
    """Non-test paths: any credential-looking literal is a finding. Test paths:
    throwaway passwords for freshly generated users are fixtures, but a
    literal password paired with a STABLE account (admin@…, non-generated
    email) or any retired literal is a finding — that is exactly the class
    of leak round 10 P0-02 found (tests/round9_live_test.py)."""
    allow = _allowlist()
    if any(path.startswith(a) or path == a for a in allow):
        return
    test_path = _is_test_path(path)
    for i, line in enumerate(text.splitlines(), 1):
        low = line.lower()
        if "secret_scan" in path and "RETIRED_LITERALS" in line:
            continue
        for lit in RETIRED_LITERALS:
            # a retired literal counts only where it could authenticate or be
            # copied into a config: quoted/assigned, not prose that documents its refusal
            if lit in line and "secret_scan" not in path and not path.endswith(".md") \
                    and re.search(rf"['\"]{lit}['\"]|[=:]\s*{lit}\b", line) \
                    and not re.search(r"(?i)refuse|reject|retired|forbid|must not|never|grep|scan", line):
                findings.append({"path": path, "line": i, "kind": "retired-literal", "match": lit})
        m = LOGIN_BODY.search(line)
        if m and _suspicious(m.group(2)) and "{" not in m.group(1):
            kind = "retired-rotated" if _rotated(m.group(2)) else "login-credential-pair"
            findings.append({"path": path, "line": i, "kind": kind, "match": m.group(1)})
            continue
        if STABLE_ACCOUNT_HINT.search(line) and re.search(rf"(?i){CRED_NAME}\s*[:=]\s*['\"][^'\"]{{6,}}['\"]", line):
            findings.append({"path": path, "line": i, "kind": "stable-account-credential", "match": "admin/ops literal"})
            continue
        # prose-form pair "admin@host / P4ssw0rd…" (test_reports/iteration_221.json leak, 2026-10-03):
        # a stable account email followed by a separator and a password-looking token
        m = STABLE_PAIR_PROSE.search(line)
        if m:
            tok = m.group(2).rstrip(".;,:)")
            sus = _suspicious(tok)
            if sus and not re.search(r"(?i)redacted|rotated|retired|refuse|password|env|\.md$", tok):
                kind = sus if sus in INFO_KINDS else "stable-account-credential"
                findings.append({"path": path, "line": i, "kind": kind, "match": m.group(1)})
                continue
        if test_path:
            continue
        for rx in (ASSIGN, JSON_KEY):
            for m in rx.finditer(line):
                kind = _suspicious(m.group(1))
                if kind and "nosec" not in low:
                    findings.append({"path": path, "line": i, "kind": kind, "match": m.group(1)[:4] + "…"})


def scan_tree(root: str) -> list:
    findings: list = []
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
        for f in files:
            full = os.path.join(d, f)
            rel = os.path.relpath(full, root)
            ext = os.path.splitext(f)[1]
            if ext not in TEXT_EXT and not f.startswith(".env") and not f.startswith("Dockerfile"):
                continue
            if f.startswith(".env") and not f.endswith(".example"):
                continue                                    # runtime env files are deployment inputs, not source
            try:
                if os.path.getsize(full) > 2_000_000:
                    continue
                text = open(full, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            scan_text(text, rel, findings)
    return findings


def scan_history(root: str, max_commits: int = 400) -> list:
    findings: list = []
    try:
        shas = subprocess.check_output(["git", "-C", root, "rev-list", f"--max-count={max_commits}", "HEAD"],
                                       text=True).split()
    except subprocess.CalledProcessError:
        return findings
    seen = set()
    for sha in shas:
        try:
            diff = subprocess.check_output(["git", "-C", root, "show", "--format=", "--unified=0", sha],
                                           text=True, errors="ignore")
        except subprocess.CalledProcessError:
            continue
        cur, chunks = None, {}
        for l in diff.splitlines():
            if l.startswith("+++ "):
                cur = l[6:] if l.startswith("+++ b/") else None
            elif cur and l.startswith("+") and not l.startswith("+++"):
                chunks.setdefault(cur, []).append(l[1:])
        for path, lines in chunks.items():
            tmp: list = []
            scan_text("\n".join(lines), path, tmp)
            for f in tmp:
                key = (f["kind"], f["match"], path)
                if key not in seen:
                    seen.add(key)
                    findings.append({**f, "path": f"git:{sha[:10]}:{path}"})
    return findings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=ROOT)
    ap.add_argument("--history", action="store_true")
    ap.add_argument("--json")
    a = ap.parse_args()
    tree = scan_tree(a.root)
    hist = scan_history(a.root) if a.history else []
    # working tree: EVERY finding blocks (retired literals must not be re-introduced either) —
    # except testing-agent reports, where a retired/rotated value is a record of a past rotation
    # (unusable); any LIVE credential pair in a report blocks (iteration_221 leak, 2026-10-03).
    # history: rotated/retired values are informational — they cannot authenticate; anything else blocks.
    def _report_record(f):
        return f["path"].startswith("test_reports/") and f["kind"] in INFO_KINDS
    blocking = [f for f in tree if not _report_record(f)] + [f for f in hist if f["kind"] not in INFO_KINDS]
    info = [f for f in tree if _report_record(f)] + [f for f in hist if f["kind"] in INFO_KINDS]
    if a.json:
        json.dump({"blocking": blocking, "informational_rotated": info, "count": len(blocking)}, open(a.json, "w"), indent=2)
    for f in blocking:
        print(f"SECRET? {f['path']}:{f['line']} {f['kind']} {f['match']}")
    if info:
        print(f"secret_scan: {len(info)} historical finding(s) match ROTATED/retired credentials (unusable) — informational")
    print(f"secret_scan: {len(blocking)} blocking finding(s)")
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())
