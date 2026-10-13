#!/usr/bin/env python3
"""Commit-message credential scan — SECURITY 2026-10-13.

Commit e17db269 carried a live SECURITY_AGENT_TELEGRAM_BOT_TOKEN=<value> line in its MESSAGE on a public repo (the
agent's finish summary had been used verbatim as the commit message); the bot was hijacked for spam. File scanners
(secret_scan.py, gitleaks git mode) never look at commit messages, so this script does: every commit message in the
range is checked for known token formats AND for env-style `NAME=value` / `name: value` lines whose name says
credential and whose value is not a placeholder (<set>, ***, …).

    python3 scripts/commit_message_secret_scan.py [--range BASE..HEAD] [--all] [--json out.json]

Exit 1 on any blocking finding. Values are NEVER printed — only the commit, the line number and the credential name.
Known, ROTATED leaks are listed by sha256 in scripts/retired_credentials.sha256 and reported as informational.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from secret_scan import PLACEHOLDERS, UPPER_PLACEHOLDER, _entropy, _rotated  # noqa: E402

CRED_NAME = re.compile(r"(?i)(pass(word|wd)?|passwd|pwd|secret|token|api[_-]?key|private[_-]?key|credential|"
                       r"bot_token|signing[_-]?key|master[_-]?key|_key$|^key$)")
# well-known credential shapes — a hit is a finding regardless of the surrounding text
TOKEN_FORMATS = {
    "telegram-bot-token": re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
    "emergent-universal-key": re.compile(r"\bsk-emergent-[A-Za-z0-9]{8,}\b"),
    "openai-key": re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}\b"),
    "stripe-key": re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}\b"),
    "resend-key": re.compile(r"\bre_[A-Za-z0-9]{20,}\b"),
    "github-token": re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    "aws-access-key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "slack-token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    "google-api-key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    "private-key-block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}
# env / yaml / prose assignment with a credential-looking NAME: `X_TOKEN=abc`, `password: abc`, `API key = abc`
ASSIGN = re.compile(r"(?im)^\s*[-*`\"']*\s*([A-Za-z][A-Za-z0-9_.\- ]{1,60}?)\s*[:=]\s*[`\"']?([^\s`\"',;]{8,})[`\"']?")
INFO_KINDS = {"retired-rotated"}


_PLACEHOLDER_WORDS = re.compile(r"(?i)^(redacted|rotated|revoked|set|unset|hidden|masked|changeme|example|placeholder|"
                                r"from[_-]env|see[_-]env|env|none|null|true|false|\d{1,4}|v?\d+(\.\d+)+|[a-z][a-z-]*)$")
_PLACEHOLDER_SHAPES = re.compile(r"^(<[^>]*>|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|\*{3,}|•{3,}|x{4,}|X{4,}|\.{3,}|…)$")


def _placeholder(v: str) -> bool:
    v = v.strip()
    return bool(PLACEHOLDERS.match(v) or UPPER_PLACEHOLDER.match(v) or _PLACEHOLDER_SHAPES.match(v) or _PLACEHOLDER_WORDS.match(v))


def _credential_like(v: str) -> bool:
    v = v.strip()
    if _placeholder(v) or v.startswith(("http://", "https://", "/", "./", "../")) or "/" in v and " " not in v and "." in v.split("/")[0]:
        return False
    if re.search(r"[()\[\]{}<>]|=[^=]", v):                                 # code / nested assignment / template
        return False
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.\-]*", v):                         # identifier-shaped: needs Upper+lower+digit
        return bool(re.search(r"[A-Z]", v) and re.search(r"[a-z]", v) and re.search(r"[0-9]", v)) and _entropy(v) >= 3.0
    return len(v) >= 8 and _entropy(v) >= 2.5 and bool(re.search(r"[A-Za-z]", v)) and bool(re.search(r"[0-9#!@$%^&*_\-+/=:]", v))


def scan_message(sha: str, message: str) -> list:
    findings = []
    for i, line in enumerate(message.splitlines(), 1):
        for kind, rx in TOKEN_FORMATS.items():
            for m in rx.finditer(line):
                val = m.group(0)
                findings.append({"commit": sha[:12], "line": i, "kind": "retired-rotated" if _rotated(val) else kind,
                                 "name": kind, "fingerprint": hashlib.sha256(val.encode()).hexdigest()[:12]})
        for m in ASSIGN.finditer(line):
            name, val = m.group(1).strip(), m.group(2).strip().rstrip(".,;:)")
            if not CRED_NAME.search(name) or not _credential_like(val):
                continue
            if any(rx.search(val) for rx in TOKEN_FORMATS.values()):
                continue                                                    # already reported above
            findings.append({"commit": sha[:12], "line": i, "kind": "retired-rotated" if _rotated(val) else "credential-assignment",
                             "name": name[:40], "fingerprint": hashlib.sha256(val.encode()).hexdigest()[:12]})
    return findings


def _commits(rng: str | None, all_history: bool) -> list:
    if all_history or not rng:
        args = ["rev-list", "HEAD"]
    else:
        args = ["rev-list", rng]
    try:
        return subprocess.check_output(["git", "-C", ROOT, *args], text=True).split()
    except subprocess.CalledProcessError:
        return []


def scan_range(rng: str | None, all_history: bool) -> list:
    findings = []
    for sha in _commits(rng, all_history):
        try:
            msg = subprocess.check_output(["git", "-C", ROOT, "log", "-1", "--format=%B", sha], text=True, errors="ignore")
        except subprocess.CalledProcessError:
            continue
        findings.extend(scan_message(sha, msg))
    return findings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--range", help="git revision range, e.g. origin/main..HEAD (default: whole history)")
    ap.add_argument("--all", action="store_true", help="scan every commit reachable from HEAD")
    ap.add_argument("--message-file", help="scan ONE message from a file (commit-msg hook)")
    ap.add_argument("--json")
    a = ap.parse_args()
    if a.message_file:
        findings = scan_message("WORKTREE", open(a.message_file, encoding="utf-8", errors="ignore").read())
    else:
        findings = scan_range(a.range, a.all)
    blocking = [f for f in findings if f["kind"] not in INFO_KINDS]
    info = [f for f in findings if f["kind"] in INFO_KINDS]
    if a.json:
        json.dump({"blocking": blocking, "informational": info}, open(a.json, "w"), indent=2)
    for f in blocking:
        print(f"BLOCK  commit {f['commit']} line {f['line']}: {f['kind']} ({f['name']}) fp={f['fingerprint']}")
    if info:
        print(f"commit-message scan: {len(info)} historical finding(s) match ROTATED credentials (informational)")
    print(f"commit-message scan: {len(blocking)} blocking finding(s)")
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())
